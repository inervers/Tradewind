import type { FollowupChannel, FollowupOutcome, FollowupTimelineItem } from "./types";

export const CHANNEL_LABEL: Record<FollowupChannel, string> = { email: "邮件", whatsapp: "WhatsApp", phone: "电话", other: "其他" };
export const OUTCOME_LABEL: Record<FollowupOutcome, string> = {
  awaiting_reply: "等待回复", replied: "收到回复", in_progress: "洽谈中",
  not_interested: "暂不感兴趣", invalid: "联系方式无效", note: "仅作备注",
};
export const DUE_LABEL = { overdue: "逾期", today: "今日", upcoming: "待跟进", unplanned: "未安排" };
export function localDateTime(now = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}T${p(now.getHours())}:${p(now.getMinutes())}`;
}
export function contactTime(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
export function eventLabel(item: FollowupTimelineItem): string {
  if (item.kind === "record") return `${CHANNEL_LABEL[item.channel]} · ${item.outcome ? OUTCOME_LABEL[item.outcome] : "记录"}`;
  const labels: Record<string, string> = {
    plan_created: "新增跟进安排", plan_rescheduled: "调整跟进安排", plan_completed: "安排已完成", plan_cancelled: "安排已取消",
    outreach_created: "外联文案加入审核队列", outreach_opened: "已打开 WhatsApp（不代表已发送）",
    outreach_accepted: "SMTP 已接受提交（不代表送达）", outreach_sent_manual: "人工确认 WhatsApp 已发送",
    outreach_failed: "外联提交失败", outreach_unknown: "外联发送结果待确认",
    outreach_submission_recorded: "记录了提交时间（结果以外联队列为准）", outreach_replied: "人工标记已回复",
  };
  return labels[item.kind] || "已记录事件";
}
