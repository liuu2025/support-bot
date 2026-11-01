import { ArrowRight, Eye, KeyRound, Scale, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { friendlyError } from "../api";
import { useToast } from "../components/Toaster";
import { Button, Card, Spinner } from "../components/ui";
import { startDemo } from "../lib/demo";
import { useMeta } from "../lib/meta";

const FEATURES = [
  {
    icon: KeyRound,
    title: "订单只对本人可见",
    text: "身份由登录凭据确认，聊天里声称自己是谁不会改变订单权限。",
  },
  {
    icon: Scale,
    title: "先核对政策，再给出结果",
    text: "特价商品、退货期限和重复退款都会由后端逐项校验。",
  },
  {
    icon: ShieldAlert,
    title: "例外申请转交人工",
    text: "符合条件且超过自动审批上限的申请进入人工工单，处理结果同步给客户。",
  },
  {
    icon: Eye,
    title: "每一步都可以回看",
    text: "订单查询、政策核验和处理结果会显示在执行记录中。",
  },
];

export default function Welcome() {
  const [starting, setStarting] = useState(false);
  const navigate = useNavigate();
  const toast = useToast();
  const { meta } = useMeta();
  const ttl = meta?.demo?.data_ttl_hours ?? 24;
  const keepDays = meta?.demo?.transcript_retention_days ?? 90;

  async function start() {
    setStarting(true);
    try {
      await startDemo();
      navigate("/chat");
    } catch (err) {
      toast(friendlyError(err), "error");
      setStarting(false);
    }
  }

  return (
    <div className="flex h-full overflow-y-auto">
      <div className="relative m-auto flex max-w-5xl flex-col gap-10 px-5 py-12 lg:flex-row lg:items-center">
        <div className="relative flex-1 animate-fade-up">
          <span className="inline-flex items-center gap-2 rounded-full border border-line bg-surface px-3 py-1 text-xs font-medium text-muted">
            <span className="h-1.5 w-1.5 rounded-full bg-ok" /> 独立体验空间 ·
            无需注册
          </span>
          <h1 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
            售后申请，从这里开始。{" "}
            <span className="text-brand">让处理进度清楚可见。</span>
          </h1>
          <p className="mt-4 max-w-xl text-base text-muted">
            系统会为你创建独立的体验客户和五笔示例订单。你可以提交退款申请，查看自动处理、拒绝和转人工的完整流程。
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button
              variant="primary"
              size="lg"
              onClick={start}
              disabled={starting}
            >
              {starting ? <Spinner /> : null}
              {starting ? "正在创建体验空间…" : "进入体验空间"}
              {!starting && <ArrowRight size={16} />}
            </Button>
            <span className="max-w-xs text-xs text-subtle">
              体验订单在 {ttl} 小时后清理，对话记录保留 {keepDays}{" "}
              天用于问题回溯。请使用示例信息。
            </span>
          </div>
        </div>

        <div className="relative grid flex-1 gap-3 sm:grid-cols-2">
          {FEATURES.map(({ icon: Icon, title, text }, i) => (
            <div
              key={title}
              className="animate-fade-up"
              style={{ animationDelay: `${i * 70}ms` }}
            >
              <Card interactive className="h-full p-4">
                <div className="mb-3 flex h-9 w-9 items-center justify-center rounded-lg bg-brand/10 text-brand">
                  <Icon size={18} />
                </div>
                <h2 className="text-sm font-semibold">{title}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  {text}
                </p>
              </Card>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
