import { useEffect, useState } from "react";
import { api } from "@/lib/api";

interface BillingStatus {
  plan: string;
  is_admin: boolean;
  free_quota_remaining: number;
  subscription: unknown;
  stripe_enabled: boolean;
}

export default function Billing() {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  useEffect(() => {
    api<BillingStatus>("/api/billing/status").then(setStatus).catch(console.error);
  }, []);
  if (!status) return <div className="text-ink-3">加载中…</div>;
  return (
    <div>
      <h1 className="font-display text-2xl font-semibold tracking-tight mb-4">账户 · 额度</h1>
      <dl className="grid grid-cols-[160px_1fr] gap-y-3 text-[13px]">
        <dt className="text-ink-3">当前套餐</dt>
        <dd className="font-medium">{status.plan}</dd>
        <dt className="text-ink-3">剩余免费次数</dt>
        <dd className="font-mono">{status.free_quota_remaining}</dd>
        <dt className="text-ink-3">管理员</dt>
        <dd>{status.is_admin ? "是（绕过所有计费）" : "否"}</dd>
        <dt className="text-ink-3">Stripe 支付</dt>
        <dd>{status.stripe_enabled ? "已启用" : "未启用（内部使用）"}</dd>
      </dl>
    </div>
  );
}
