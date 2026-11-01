import {
  AlertTriangle,
  CheckCircle2,
  Cpu,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  applyModelPreset,
  clearModelConfig,
  deleteModelPreset,
  fetchModelConfig,
  fetchModelOptions,
  fetchModelPresets,
  friendlyError,
  renameModelPreset,
  saveModelConfig,
  saveModelPreset,
  testModelConfig,
} from "../api";
import { useToast } from "../components/Toaster";
import { Badge, Button, Card, Spinner } from "../components/ui";
import type { ModelConfig, ModelPreset } from "../types";
import { useMeta } from "../lib/meta";
import { onTokenChange } from "../auth";

/**
 * 模型设置 —— 全站唯一一处决定"智能体用哪个大模型"的地方。
 *
 * 只支持 OpenAI 兼容协议（官方接口、DeepSeek、GLM、硅基流动、中转站都是同一个
 * 协议），所以只需要三件事：服务地址、API Key、模型。模型不由人手填：点「获取模型」
 * 后由服务端向 `{地址}/models` 拉取，从下拉里选。
 *
 * 保存后立即生效（后端按配置版本号重建客户端与图），不需要重启容器。
 */

const FIELD =
  "h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm outline-none transition-colors focus:border-brand focus:ring-2 focus:ring-brand/20 disabled:opacity-60";

function Label({
  children,
  hint,
  htmlFor,
}: {
  children: React.ReactNode;
  hint?: string;
  htmlFor?: string;
}) {
  return (
    <div className="mb-1.5 flex items-baseline gap-2">
      <label
        htmlFor={htmlFor}
        className="shrink-0 whitespace-nowrap text-xs font-medium text-fg"
      >
        {children}
      </label>
      {hint && <span className="text-[11px] text-subtle">{hint}</span>}
    </div>
  );
}

export default function Settings() {
  const toast = useToast();
  const { refresh } = useMeta();
  const requestSequence = useRef(0);
  const [clearConfirm, setClearConfirm] = useState(false);
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [modelName, setModelName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [timeout, setTimeoutS] = useState(60);
  const [models, setModels] = useState<string[]>([]);
  const [presets, setPresets] = useState<ModelPreset[]>([]);
  const [busy, setBusy] = useState<"load" | "models" | "save" | "test" | null>(
    "load",
  );
  const [presetName, setPresetName] = useState("");
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(
    null,
  );
  const [probe, setProbe] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setBusy("load");
    setError(null);
    try {
      const [cfg, list] = await Promise.all([
        fetchModelConfig(),
        fetchModelPresets(),
      ]);
      if (sequence !== requestSequence.current) return;
      setConfig(cfg);
      setApiKey("");
      setBaseUrl(cfg.base_url);
      setModelName(cfg.model_name);
      setTimeoutS(Math.round(cfg.timeout_s));
      setModels(cfg.model_name ? [cfg.model_name] : []);
      setPresets(list);
    } catch (err) {
      if (sequence !== requestSequence.current) return;
      setError(
        err instanceof ApiError && err.status === 403
          ? "当前身份没有修改模型配置的权限。"
          : friendlyError(err),
      );
    } finally {
      if (sequence === requestSequence.current) setBusy(null);
    }
  }, []);

  useEffect(() => {
    void load();
    return onTokenChange((role) => {
      if (role === "admin") void load();
    });
  }, [load]);

  async function onFetchModels() {
    if (!baseUrl.trim()) {
      toast("请先填写服务地址", "error");
      return;
    }
    const sequence = ++requestSequence.current;
    setBusy("models");
    setError(null);
    try {
      const list = await fetchModelOptions(baseUrl.trim(), apiKey.trim());
      if (sequence !== requestSequence.current) return;
      setModels(list);
      if (list.length === 0) {
        toast("这个地址没有返回任何模型", "error");
      } else {
        toast(`获取到 ${list.length} 个模型`, "success");
        if (!list.includes(modelName)) setModelName(list[0]);
      }
    } catch (err) {
      if (sequence === requestSequence.current) {
        setModels([]);
        setModelName("");
        setError(friendlyError(err));
      }
    } finally {
      setBusy(null);
    }
  }

  async function onSave() {
    if (!modelName) {
      toast("请先获取并选择模型", "error");
      return;
    }
    setBusy("save");
    setError(null);
    try {
      const saved = await saveModelConfig({
        base_url: baseUrl.trim(),
        model_name: modelName,
        api_key: apiKey.trim(),
        timeout_s: timeout,
      });
      setConfig(saved);
      await refresh().catch(() =>
        toast("配置已保存，页头刷新失败，请刷新页面", "error"),
      );
      setBaseUrl(saved.base_url);
      setApiKey("");
      setProbe(null);
      toast(`已保存，立即生效（配置版本 v${saved.version}）`, "success");
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onTest() {
    setBusy("test");
    setError(null);
    setProbe(null);
    try {
      const result = await testModelConfig();
      setProbe(result.detail);
      toast("连接正常：模型能正确调用工具", "success");
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onSavePreset() {
    const name = presetName.trim();
    if (!name) {
      toast("给这个方案起个名字", "error");
      return;
    }
    setBusy("save");
    try {
      await saveModelPreset({
        name,
        base_url: baseUrl.trim(),
        model_name: modelName,
        api_key: apiKey.trim(),
        timeout_s: timeout,
      });
      setPresetName("");
      setPresets(await fetchModelPresets());
      toast(`已存为方案「${name}」`, "success");
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onApply(preset: ModelPreset) {
    requestSequence.current += 1;
    setBusy("save");
    try {
      const saved = await applyModelPreset(preset.id);
      setConfig(saved);
      await refresh().catch(() =>
        toast("配置已保存，页头刷新失败，请刷新页面", "error"),
      );
      setBaseUrl(saved.base_url);
      setModelName(saved.model_name);
      setTimeoutS(Math.round(saved.timeout_s));
      setModels([saved.model_name]);
      setApiKey("");
      setProbe(null);
      toast(`已应用方案「${preset.name}」`, "success");
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onRename() {
    if (!renaming) return;
    setBusy("save");
    try {
      await renameModelPreset(renaming.id, renaming.name.trim());
      setRenaming(null);
      setPresets(await fetchModelPresets());
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onDelete(preset: ModelPreset) {
    if (
      !window.confirm(
        `删除方案「${preset.name}」？当前生效的模型配置不受影响。`,
      )
    )
      return;
    setBusy("save");
    try {
      await deleteModelPreset(preset.id);
      setPresets(await fetchModelPresets());
      toast(`已删除方案「${preset.name}」`, "success");
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  async function onClear() {
    setBusy("save");
    try {
      await clearModelConfig();
      await refresh().catch(() => {});
      setClearConfirm(false);
      setConfig(null);
      setBaseUrl("");
      setModelName("");
      setApiKey("");
      setModels([]);
      setProbe(null);
      toast("已清除模型配置", "success");
      await load();
    } catch (err) {
      setError(friendlyError(err));
    } finally {
      setBusy(null);
    }
  }

  if (busy === "load" && !config) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted">
        <Spinner className="text-brand" /> 正在读取模型配置…
      </div>
    );
  }

  const unconfigured = !config?.configured;
  const dirty =
    Boolean(apiKey.trim()) ||
    baseUrl.trim().replace(/\/+$/, "") !==
      (config?.base_url || "").replace(/\/+$/, "") ||
    modelName !== config?.model_name ||
    timeout !== config?.timeout_s;
  const invalidateOptions = () => {
    requestSequence.current += 1;
    setModels([]);
    setModelName("");
    setProbe(null);
  };

  return (
    <div className="mx-auto w-full max-w-4xl space-y-5 px-4 py-7">
      <div className="settings-hero motion-card motion-enter" data-spotlight>
        <div className="mb-3 text-[10px] tracking-[0.2em] text-brand">
          连接你的模型服务
        </div>
        <h1>服务接入，一次配置即可使用。</h1>
        <p className="mt-1 text-sm text-muted">
          支持任何 OpenAI 兼容接口（官方
          OpenAI、DeepSeek、GLM、硅基流动、中转站等）。
          保存后下一轮对话立即生效，不需要重启服务。
        </p>
        <div className="settings-step-track">
          <span>01 填写地址</span>
          <span>02 填写密钥</span>
          <span>03 获取并选择模型</span>
          <span>04 保存与测试</span>
        </div>
      </div>

      {unconfigured && (
        <Card className="flex items-start gap-3 border-warn/40 bg-warn/5 px-4 py-3">
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-warn" />
          <div className="text-sm">
            <div className="font-medium text-warn">还没有可用的模型</div>
            <p className="mt-0.5 text-muted">
              在下面填写服务地址与 API
              Key，点「获取模型」选一个模型保存，客服助手才能回答。
            </p>
          </div>
        </Card>
      )}

      {error && (
        <Card className="flex items-start gap-3 border-bad/40 bg-bad/5 px-4 py-3 text-sm">
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-bad" />
          <span className="flex-1 text-bad">{error}</span>
          <button
            onClick={() => setError(null)}
            className="text-subtle hover:text-fg"
            aria-label="关闭"
          >
            <X size={14} />
          </button>
        </Card>
      )}

      {probe && (
        <Card className="flex items-start gap-3 border-ok/40 bg-ok/5 px-4 py-3 text-sm">
          <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-ok" />
          <span className="flex-1 text-ok">{probe}</span>
        </Card>
      )}

      <Card className="space-y-5 p-5">
        <div className="flex items-center gap-2">
          <Cpu size={16} className="text-brand" />
          <h2 className="text-sm font-semibold">生成模型</h2>
          {config?.configured && (
            <Badge tone="ok" className="ml-auto">
              已配置 · v{config.version}
            </Badge>
          )}
          {config?.configured && config.source === "env" && (
            <Badge tone="neutral">来自环境变量</Badge>
          )}
        </div>

        <div>
          <Label
            htmlFor="model-base-url"
            hint="完整地址以 http:// 或 https:// 开头；只填域名自动补 /v1"
          >
            服务地址
          </Label>
          <input
            className={FIELD}
            id="model-base-url"
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value);
              setApiKey("");
              invalidateOptions();
            }}
            placeholder="https://api.deepseek.com/v1"
            spellCheck={false}
            autoComplete="off"
          />
        </div>

        <div>
          <Label
            htmlFor="model-api-key"
            hint={
              config?.has_api_key
                ? "同一地址可留空沿用；更换地址需重新填写"
                : undefined
            }
          >
            API Key
          </Label>
          <input
            className={FIELD}
            type="password"
            id="model-api-key"
            value={apiKey}
            onChange={(e) => {
              setApiKey(e.target.value);
              invalidateOptions();
            }}
            placeholder={
              config?.has_api_key ? "已配置，留空保持不变" : "sk-..."
            }
            autoComplete="new-password"
            spellCheck={false}
          />
          <p className="mt-1 text-[11px] text-subtle">
            密钥加密后保存在数据库里，接口只返回"是否已配置"，不会回显明文。
          </p>
        </div>

        <div>
          <Label
            htmlFor="model-name"
            hint="点「获取模型」从服务端拉取，不用手填"
          >
            模型名称
          </Label>
          <div className="flex gap-2">
            <select
              className={FIELD}
              id="model-name"
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              disabled={models.length === 0 || busy !== null}
            >
              {models.length === 0 && <option value="">请先获取模型</option>}
              {models.length > 0 && !modelName && (
                <option value="">请选择模型</option>
              )}
              {models.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
            <Button
              onClick={onFetchModels}
              disabled={busy !== null || !baseUrl.trim()}
              className="shrink-0"
            >
              {busy === "models" ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              获取模型
            </Button>
          </div>
        </div>

        <details className="rounded-lg border border-line px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-muted select-none">
            高级设置
          </summary>
          <div className="mt-3">
            <Label htmlFor="model-timeout" hint="单次请求的超时（秒）">
              超时时间
            </Label>
            <input
              className={FIELD}
              type="number"
              min={5}
              max={600}
              id="model-timeout"
              value={timeout}
              onChange={(e) => setTimeoutS(Number(e.target.value) || 60)}
            />
          </div>
        </details>

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          <Button
            variant="primary"
            onClick={onSave}
            disabled={busy !== null || !modelName || !baseUrl.trim()}
          >
            {busy === "save" ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Save size={14} />
            )}
            保存
          </Button>
          <Button
            onClick={onTest}
            disabled={busy !== null || !config?.configured || dirty}
          >
            {busy === "test" ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <PlugZap size={14} />
            )}
            测试已保存配置
          </Button>
          {config?.source === "db" && (
            <Button
              variant="ghost"
              onClick={() => setClearConfirm(true)}
              disabled={busy !== null}
              className="ml-auto text-bad"
            >
              <Trash2 size={14} /> 清除配置
            </Button>
          )}
        </div>
        <p className="text-[11px] text-subtle">
          请先保存当前改动，再测试连接。测试会向已保存的模型发送一次工具调用请求，检查它是否支持查询订单等操作。
        </p>
      </Card>

      {clearConfirm && (
        <Card className="space-y-3 border-warn/40 p-5">
          <p className="text-sm">
            清除已保存的配置和密钥后，将恢复环境变量中的模型配置；如果没有环境配置，客服将无法回答。
          </p>
          <div className="flex gap-2">
            <Button
              disabled={busy !== null}
              onClick={() => {
                void onClear();
              }}
            >
              确认清除
            </Button>
            <Button variant="ghost" onClick={() => setClearConfirm(false)}>
              取消
            </Button>
          </div>
        </Card>
      )}
      <Card className="space-y-3 p-5">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">配置方案</h2>
          <span className="text-[11px] text-subtle">
            常用的地址存成方案，一键切换
          </span>
        </div>

        <div className="flex gap-2">
          <input
            className={FIELD}
            aria-label="方案名称"
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder="给当前配置起个名字，例如「中转站」"
          />
          <Button
            onClick={onSavePreset}
            disabled={busy !== null || !baseUrl || !modelName}
            className="shrink-0"
          >
            <Plus size={14} /> 存为方案
          </Button>
        </div>

        {presets.length === 0 ? (
          <p className="py-3 text-center text-xs text-subtle">
            还没有保存过方案
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {presets.map((preset) => (
              <li
                key={preset.id}
                className="flex flex-wrap items-center gap-2 py-2.5"
              >
                {renaming?.id === preset.id ? (
                  <>
                    <input
                      className={`${FIELD} max-w-[12rem]`}
                      value={renaming.name}
                      onChange={(e) =>
                        setRenaming({ id: preset.id, name: e.target.value })
                      }
                    />
                    <Button size="sm" onClick={onRename}>
                      确定
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setRenaming(null)}
                    >
                      取消
                    </Button>
                  </>
                ) : (
                  <>
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">
                        {preset.name}
                      </div>
                      <div className="truncate text-[11px] text-subtle">
                        {preset.model_name} · {preset.base_url}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => onApply(preset)}
                    >
                      应用
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        setRenaming({ id: preset.id, name: preset.name })
                      }
                      aria-label="改名"
                    >
                      <Pencil size={13} />
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-bad"
                      onClick={() => onDelete(preset)}
                      aria-label="删除"
                    >
                      <Trash2 size={13} />
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
