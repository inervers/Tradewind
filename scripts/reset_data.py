"""清空旧市场测试数据（欧美），保留代码、产品资料与邮件模板。

用途：市场转向香港前的一次性清理。
执行：python scripts/reset_data.py
效果：
  1. customers.json 置空（保留空表结构）
  2. 删除爬虫产物 maps_*.csv / customers_*.csv
  3. 清空历史邮件表（tradewind_memory.db）——避免欧美话术污染香港参考
"""

from __future__ import annotations

import glob
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> None:
    print("== Tradewind 旧市场数据清理 ==")

    # 1. 客户名单置空
    cf = DATA / "customers.json"
    if cf.exists():
        cf.write_text("[]", encoding="utf-8")
        print("✓ customers.json 已置空")
    else:
        print("- customers.json 不存在，跳过")

    # 2. 爬虫产物（CSV 含真实客户信息，gitignore 但本机要清）
    removed = 0
    for pat in ("maps_*.csv", "customers_*.csv"):
        for f in glob.glob(str(DATA / pat)):
            Path(f).unlink()
            removed += 1
            print(f"✓ 删除 {Path(f).name}")
    if not removed:
        print("- 无爬虫 CSV")

    # 3. 历史邮件清空
    db = DATA / "tradewind_memory.db"
    if db.exists():
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("DELETE FROM emails")
            conn.commit()
            print("✓ 历史邮件已清空")
        finally:
            conn.close()
    else:
        print("- 记忆库不存在，跳过")

    print("== 清理完成 ==")


if __name__ == "__main__":
    main()
