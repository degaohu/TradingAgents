import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";

export default function NewAnalysis() {
  const navigate = useNavigate();
  const [ticker, setTicker] = useState("600519.SS");
  const [tradeDate, setTradeDate] = useState(new Date().toISOString().slice(0, 10));
  const [provider, setProvider] = useState("openai");
  const [deepLlm, setDeepLlm] = useState("gpt-5.5");
  const [quickLlm, setQuickLlm] = useState("gpt-5.4-mini");
  const [rounds, setRounds] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      const res = await api<{ job_id: string }>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          ticker,
          trade_date: tradeDate,
          provider,
          deep_llm: deepLlm,
          quick_llm: quickLlm,
          max_debate_rounds: rounds,
        }),
      });
      navigate(`/jobs/${res.job_id}`);
    } catch (e) {
      setErr(String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className="max-w-lg">
      <h1 className="font-display text-2xl font-semibold tracking-tight mb-1">新建分析</h1>
      <p className="text-ink-3 text-[13px] mb-6">
        提交后会进入队列，VPS worker 拉起后开始跑，进度通过 SSE 实时推送。
      </p>

      <form onSubmit={submit} className="space-y-4">
        <Field label="股票代码">
          <input
            className="input"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            required
          />
        </Field>
        <Field label="分析日期">
          <input
            type="date"
            className="input"
            value={tradeDate}
            onChange={(e) => setTradeDate(e.target.value)}
            required
          />
        </Field>
        <Field label="LLM 提供商">
          <select className="input" value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="openai">OpenAI</option>
            <option value="anthropic">Anthropic</option>
            <option value="deepseek">DeepSeek</option>
            <option value="google">Gemini</option>
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-4">
          <Field label="深度思考模型">
            <input className="input" value={deepLlm} onChange={(e) => setDeepLlm(e.target.value)} />
          </Field>
          <Field label="快速推理模型">
            <input className="input" value={quickLlm} onChange={(e) => setQuickLlm(e.target.value)} />
          </Field>
        </div>
        <Field label="最大辩论轮次">
          <input
            type="number"
            min={1}
            max={4}
            className="input"
            value={rounds}
            onChange={(e) => setRounds(Number(e.target.value))}
          />
        </Field>

        {err && <div className="text-neg text-sm">{err}</div>}

        <button
          type="submit"
          disabled={submitting}
          className="w-full py-2.5 rounded-md bg-ink text-white font-medium disabled:opacity-50"
        >
          {submitting ? "提交中…" : "开始分析"}
        </button>
      </form>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[11px] uppercase tracking-wider font-semibold text-ink-3 mb-1.5">
        {label}
      </span>
      {children}
    </label>
  );
}
