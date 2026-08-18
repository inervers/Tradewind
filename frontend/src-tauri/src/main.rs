#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    fs::{self, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, RunEvent, WindowEvent};

const BACKEND_ADDR: &str = "127.0.0.1:8101";
const HEALTH_PATH: &str = "/api/health";
const DESKTOP_INSTANCE_ADDR: &str = "127.0.0.1:48101";

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
struct BackendState {
    child: Mutex<Option<Child>>,
    owns_backend: AtomicBool,
    shutting_down: AtomicBool,
}

struct PythonCandidate {
    program: PathBuf,
    prefix_args: Vec<String>,
}

fn health_is_ready(timeout: Duration) -> bool {
    let address: SocketAddr = match BACKEND_ADDR.parse() {
        Ok(address) => address,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&address, timeout) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    let request =
        format!("GET {HEALTH_PATH} HTTP/1.1\r\nHost: {BACKEND_ADDR}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    stream.read_to_string(&mut response).is_ok()
        && (response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200"))
}

fn acquire_single_instance() -> Option<TcpListener> {
    let address: SocketAddr = DESKTOP_INSTANCE_ADDR.parse().expect("单实例地址配置错误");
    if let Ok(listener) = TcpListener::bind(address) {
        return Some(listener);
    }

    let mut acknowledged = false;
    if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(500)) {
        let _ = stream.set_read_timeout(Some(Duration::from_millis(750)));
        let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
        if stream.write_all(b"focus").is_ok() {
            let mut reply = [0_u8; 2];
            acknowledged = stream.read_exact(&mut reply).is_ok() && &reply == b"ok";
        }
    }
    if acknowledged {
        return None;
    }

    // 旧实例可能正在执行退出清理：等待它释放监听端口，避免吞掉首次重开。
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if let Ok(listener) = TcpListener::bind(address) {
            return Some(listener);
        }
        thread::sleep(Duration::from_millis(100));
    }
    None
}

fn listen_for_second_instances(listener: TcpListener, app: tauri::AppHandle) {
    for mut stream in listener.incoming().flatten() {
        let state = app.state::<BackendState>();
        if state.shutting_down.load(Ordering::SeqCst) {
            continue;
        }
        show_main_window(&app, None);
        let _ = stream.write_all(b"ok");
    }
}

fn monitor_main_window(app: tauri::AppHandle) {
    let mut was_visible = false;
    loop {
        thread::sleep(Duration::from_millis(250));
        let state = app.state::<BackendState>();
        if state.shutting_down.load(Ordering::SeqCst) {
            return;
        }
        let visible = app
            .get_webview_window("main")
            .and_then(|window| window.is_visible().ok())
            .unwrap_or(false);
        if visible {
            was_visible = true;
        } else if was_visible {
            stop_owned_backend(&app);
            app.exit(0);
            return;
        }
    }
}

fn find_project_root() -> Option<PathBuf> {
    if let Some(root) = env::var_os("TRADEWIND_PROJECT_ROOT").map(PathBuf::from) {
        if root.join("server.py").is_file() {
            return Some(root);
        }
    }

    if let Ok(executable) = env::current_exe() {
        for directory in executable.ancestors().skip(1) {
            if directory.join("server.py").is_file() {
                return Some(directory.to_path_buf());
            }
        }
    }

    let source_root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)?;
    source_root
        .join("server.py")
        .is_file()
        .then_some(source_root)
}

fn python_candidates(project_root: &Path) -> Vec<PythonCandidate> {
    let mut candidates = Vec::new();
    if let Some(python) = env::var_os("TRADEWIND_PYTHON") {
        candidates.push(PythonCandidate {
            program: PathBuf::from(python),
            prefix_args: Vec::new(),
        });
    }
    for relative in [".venv/Scripts/python.exe", "venv/Scripts/python.exe"] {
        let python = project_root.join(relative);
        if python.is_file() {
            candidates.push(PythonCandidate {
                program: python,
                prefix_args: Vec::new(),
            });
        }
    }
    candidates.push(PythonCandidate {
        program: PathBuf::from("python.exe"),
        prefix_args: Vec::new(),
    });
    candidates.push(PythonCandidate {
        program: PathBuf::from("python"),
        prefix_args: Vec::new(),
    });
    candidates.push(PythonCandidate {
        program: PathBuf::from("py.exe"),
        prefix_args: vec!["-3".to_string()],
    });
    candidates
}

fn spawn_source_backend(project_root: &Path) -> Result<Child, String> {
    let mut failures = Vec::new();
    for candidate in python_candidates(project_root) {
        let mut command = Command::new(&candidate.program);
        command
            .args(&candidate.prefix_args)
            .args([
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8101",
                "--log-level",
                "warning",
            ])
            .current_dir(project_root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        match command.spawn() {
            Ok(child) => return Ok(child),
            Err(error) => failures.push(format!("{}: {error}", candidate.program.display())),
        }
    }
    Err(format!("找不到可用的 Python：{}", failures.join("；")))
}

fn packaged_backend_candidates() -> Vec<PathBuf> {
    let Ok(executable) = env::current_exe() else {
        return Vec::new();
    };
    let Some(directory) = executable.parent() else {
        return Vec::new();
    };
    vec![
        directory.join("tradewind-backend.exe"),
        directory.join("tradewind-backend-x86_64-pc-windows-msvc.exe"),
        directory.join("backend").join("tradewind-backend.exe"),
    ]
}

fn spawn_packaged_backend(app: &tauri::AppHandle) -> Result<Option<Child>, String> {
    let Some(program) = packaged_backend_candidates()
        .into_iter()
        .find(|path| path.is_file())
    else {
        return Ok(None);
    };

    let data_root = app
        .path()
        .app_local_data_dir()
        .map_err(|error| format!("无法定位用户数据目录：{error}"))?;
    fs::create_dir_all(&data_root).map_err(|error| format!("无法创建用户数据目录：{error}"))?;
    let log_dir = data_root.join("logs");
    fs::create_dir_all(&log_dir).map_err(|error| format!("无法创建日志目录：{error}"))?;
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("backend.out.log"))
        .map_err(|error| format!("无法打开后端日志：{error}"))?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("backend.err.log"))
        .map_err(|error| format!("无法打开后端错误日志：{error}"))?;

    let mut command = Command::new(&program);
    command
        .env("TRADEWIND_DATA_DIR", &data_root)
        .env("TRADEWIND_DESKTOP", "1")
        .current_dir(program.parent().unwrap_or(&data_root))
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
        .spawn()
        .map(Some)
        .map_err(|error| format!("无法启动内置后端 {}：{error}", program.display()))
}

fn show_main_window(app: &tauri::AppHandle, startup_error: Option<&str>) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
    if let Some(message) = startup_error {
        let escaped = message
            .replace('\\', "\\\\")
            .replace('`', "\\`")
            .replace("${", "\\${")
            .replace('\r', "")
            .replace('\n', "\\n");
        let _ = window.eval(format!(
            "window.setTimeout(() => window.alert(`Tradewind 后端启动失败：\\n{escaped}`), 250);"
        ));
    }
}

fn start_backend(app: tauri::AppHandle) {
    let state = app.state::<BackendState>();
    if health_is_ready(Duration::from_millis(500)) {
        show_main_window(&app, None);
        return;
    }

    let child = match spawn_packaged_backend(&app) {
        Ok(Some(child)) => child,
        Ok(None) => {
            let Some(project_root) = find_project_root() else {
                show_main_window(&app, Some("未找到内置后端，也未找到项目源码"));
                return;
            };
            match spawn_source_backend(&project_root) {
                Ok(child) => child,
                Err(error) => {
                    show_main_window(&app, Some(&error));
                    return;
                }
            }
        }
        Err(error) => {
            show_main_window(&app, Some(&error));
            return;
        }
    };

    if state.shutting_down.load(Ordering::SeqCst) {
        terminate_process_tree(child.id());
        return;
    }
    state.owns_backend.store(true, Ordering::SeqCst);
    *state.child.lock().expect("后端进程锁异常") = Some(child);

    let deadline = Instant::now() + Duration::from_secs(60);
    while Instant::now() < deadline {
        if state.shutting_down.load(Ordering::SeqCst) {
            return;
        }
        if health_is_ready(Duration::from_millis(400)) {
            show_main_window(&app, None);
            return;
        }
        let exited = state
            .child
            .lock()
            .ok()
            .and_then(|mut child| {
                child
                    .as_mut()
                    .and_then(|process| process.try_wait().ok())
                    .flatten()
            })
            .is_some();
        if exited {
            show_main_window(
                &app,
                Some("内置后端进程已提前退出，请查看用户数据目录下的 logs"),
            );
            return;
        }
        thread::sleep(Duration::from_millis(250));
    }
    show_main_window(&app, Some("等待 http://127.0.0.1:8101/api/health 超时"));
}

#[cfg(windows)]
fn terminate_process_tree(pid: u32) {
    use std::os::windows::process::CommandExt;
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
}

#[cfg(not(windows))]
fn terminate_process_tree(_pid: u32) {}

fn stop_owned_backend(app: &tauri::AppHandle) {
    let state = app.state::<BackendState>();
    state.shutting_down.store(true, Ordering::SeqCst);
    if !state.owns_backend.swap(false, Ordering::SeqCst) {
        return;
    }
    if let Ok(mut slot) = state.child.lock() {
        if let Some(child) = slot.take() {
            terminate_process_tree(child.id());
        }
    };
}

fn main() {
    let Some(instance_listener) = acquire_single_instance() else {
        return;
    };
    let app = tauri::Builder::default()
        .manage(BackendState::default())
        .setup(|app| {
            // 首次双击立即显示 React 的“启动中”界面，后端就绪后前端自动恢复配置。
            show_main_window(app.handle(), None);
            let handle = app.handle().clone();
            thread::spawn(move || start_backend(handle));
            let handle = app.handle().clone();
            thread::spawn(move || listen_for_second_instances(instance_listener, handle));
            let handle = app.handle().clone();
            thread::spawn(move || monitor_main_window(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("启动 Tradewind 桌面窗口失败");

    app.run(|app, event| match event {
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::Destroyed,
            ..
        } if label == "main" => {
            stop_owned_backend(app);
            app.exit(0);
        }
        RunEvent::Exit | RunEvent::ExitRequested { .. } => stop_owned_backend(app),
        _ => {}
    });
}
