'use client';

import {useRef, useState} from 'react';
import {Download, FileJson, Loader2, Upload, UsersRound} from 'lucide-react';

import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {errText, platformApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';

/**
 * 平台账号的导入 / 导出弹窗（与 WorkBuddy 账号页同款交互）。
 *
 * 平台账号的「导入」有两层含义，这里把它们收在同一个入口：
 *   · 文件导入：选择导出的 JSON（单账号或 `{accounts: [...]}` 批量）；
 *   · 导出：把本平台账号（含凭据）下载成可直接再导入的 JSON。
 *
 * 导出只对管理员显示，且走二次确认——它把账号凭据打包带走，落到别人手里
 * 等于这些账号被接管。真正的权限拦截在后端（`require_session_admin` + 审计）。
 */
export function PlatformImportExportButton({
  platform,
  label,
  supportsExport,
  onPasteImport,
  onSuccess,
}: {
  platform: string;
  label: string;
  supportsExport: boolean;
  /** 打开「粘贴账号」弹窗（Dumate 的 Cookie、Loomy 的账号密码等）。 */
  onPasteImport?: () => void;
  onSuccess?: () => void;
}) {
  const t = useT();
  const {isAdmin} = useAuth();
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const canExport = isAdmin && supportsExport;

  /** 一个文件可能装一个账号，也可能是导出包的 `{accounts: [...]}`。 */
  function importBodyFor(parsed: unknown): unknown {
    if (Array.isArray(parsed)) return {accounts: parsed};
    if (parsed && typeof parsed === 'object'
        && Array.isArray((parsed as {accounts?: unknown}).accounts)) {
      return parsed;
    }
    return parsed;
  }

  async function upload(files: File[]) {
    if (!files.length || busy) return;
    setBusy(true);
    let added = 0;
    const failed: string[] = [];
    try {
      for (const file of files) {
        let parsed: unknown;
        try {
          parsed = JSON.parse(await file.text());
        } catch {
          failed.push(`${file.name}: ${t('platforms.importInvalid')}`);
          continue;
        }
        try {
          const result = await platformApi.import(platform, importBodyFor(parsed));
          added += result.imported.length;
          failed.push(...result.failed.map((item) => {
            const who = item.uid ? `${item.uid}: ` : '';
            return `${file.name}: ${who}${item.error}`;
          }));
        } catch (error) {
          failed.push(`${file.name}: ${errText(error)}`);
        }
      }
      if (added > 0) {
        notify.ok(t('platforms.uploadDone'),
                  t('platforms.importDone', {ok: added, failed: failed.length}));
        onSuccess?.();
      }
      if (failed.length) {
        notify.err(t('platforms.importDone', {ok: added, failed: failed.length}),
                   failed.join('\n'));
      }
      if (!added && !failed.length) notify.info(t('platforms.importEmpty'));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  async function exportAccounts() {
    if (busy) return;
    setBusy(true);
    try {
      const data = await platformApi.exportAccounts(platform);
      const blob = new Blob([`${JSON.stringify(data, null, 2)}\n`],
        {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${platform}-accounts-${stamp}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      notify.ok(t('platforms.exportDone'), t('platforms.exportDoneDetail'));
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept=".json,application/json"
        multiple
        className="hidden"
        onChange={(event) => void upload(Array.from(event.target.files ?? []))}
      />
      <Button
        size="sm"
        variant="outline"
        className="rounded-full"
        disabled={busy}
        onClick={() => setOpen(true)}
        title={canExport ? t('platforms.importExport') : t('platforms.importAccounts')}
      >
        {busy ? <Loader2 className="animate-spin" /> : <UsersRound />}
        <span>{canExport ? t('platforms.importExport') : t('platforms.importAccounts')}</span>
      </Button>
      <Dialog open={open} onOpenChange={(next) => { if (!busy) setOpen(next); }}>
        <DialogContent className="max-w-[520px]" showCloseButton>
          <DialogHeader>
            <DialogTitle>
              {canExport ? t('platforms.importExport') : t('platforms.importAccounts')}
            </DialogTitle>
            <DialogDescription>
              {canExport ? t('platforms.importExportDesc') : t('platforms.importFileHint')}
            </DialogDescription>
          </DialogHeader>
          <div className="flex w-full flex-col gap-4 px-6 pb-6">
            <div className={`grid gap-3 ${canExport ? 'sm:grid-cols-2' : ''}`}>
              <div className="rounded-2xl border border-border/70 p-4">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                  <Upload className="h-4 w-4" />
                  {t('platforms.importAccounts')}
                </div>
                <p className="mb-4 text-xs leading-5 text-muted-foreground">
                  {t('platforms.importFileHint')}
                </p>
                <Button
                  className="mt-2 w-full rounded-full"
                  disabled={busy}
                  onClick={() => inputRef.current?.click()}
                >
                  <Upload className="h-4 w-4" />
                  {t('platforms.importFile')}
                </Button>
                {onPasteImport && (
                  <Button
                    variant="outline"
                    className="mt-2 w-full rounded-full"
                    disabled={busy}
                    onClick={() => { setOpen(false); onPasteImport(); }}
                  >
                    <FileJson className="h-4 w-4" />
                    {t('platforms.importManual')}
                  </Button>
                )}
              </div>
              {/* 导出会把账号凭据打包带走，仅管理员可用。 */}
              {canExport && (
                <div className="rounded-2xl border border-border/70 p-4">
                  <div className="mb-3 flex items-center gap-2 text-sm font-medium">
                    <Download className="h-4 w-4" />
                    {t('platforms.exportAccounts')}
                  </div>
                  <p className="mb-4 text-xs leading-5 text-muted-foreground">
                    {t('platforms.exportAccountsDesc', {label})}
                  </p>
                  <ConfirmDialog
                    title={t('platforms.exportConfirmTitle')}
                    description={t('platforms.exportConfirmDesc')}
                    confirmText={t('platforms.exportAccounts')}
                    destructive
                    trigger={
                      <Button
                        variant="outline"
                        className="mt-2 w-full rounded-full"
                        disabled={busy}
                      >
                        <Download className="h-4 w-4" />
                        {t('platforms.exportAccounts')}
                      </Button>
                    }
                    onConfirm={() => void exportAccounts()}
                  />
                </div>
              )}
            </div>
            <Button variant="outline" className="w-full rounded-full" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}