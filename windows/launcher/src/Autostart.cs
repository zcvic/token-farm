using System;
using System.IO;
using System.Windows.Forms;
using Microsoft.Win32;

namespace TokenFarmLauncher
{
    /// 开机自启：写当前用户的 Run 键（HKCU，不需要管理员权限）。
    ///
    /// 为什么用 HKCU 而不是计划任务 / 服务：
    ///   · 只需要在用户登录后拉起来驻留托盘，不需要 SYSTEM 权限；
    ///   · 计划任务会出现在「任务计划程序」里，用户卸载后忘记清理就是残留；
    ///   · 启动器本身就是用户级程序，与服务级生命周期无关。
    ///
    /// 值形如：`"C:\...\TokenFarm.exe" --tray`。静默参数由 Program 识别，
    /// 命中时不显示主窗口（见 MainForm.SetVisibleCore），只驻留托盘。
    internal static class Autostart
    {
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string ValueName = "token-farm";

        /// 静默启动参数：不显示主窗口，只驻留托盘。
        public const string SilentArgument = "--tray";

        /// 写入注册表的值：带引号的可执行文件路径 + 静默参数。
        public static string CommandLine()
        {
            return CommandLine(Application.ExecutablePath);
        }

        public static string CommandLine(string exePath)
        {
            return "\"" + exePath + "\" " + SilentArgument;
        }

        /// 当前这份安装是否已开启自启。
        ///
        /// **必须校验路径**：偏好文件按用户共享，注册表值也是按用户唯一的；
        /// 同一台机器上装了第二份（例如测试版）时，不能因为「值存在」就显示已开启。
        public static bool IsEnabled()
        {
            string value = ReadValue();
            return !String.IsNullOrEmpty(value) &&
                value.IndexOf(Application.ExecutablePath, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        public static void Enable()
        {
            EnableFor(Application.ExecutablePath);
        }

        /// 按指定 exe 路径写自启项。
        ///
        /// 覆盖安装时调用方是**安装包**进程，`Application.ExecutablePath` 指向 setup
        /// 本身；写进去就会变成「开机弹出安装向导」。所以路径必须由调用方给出。
        public static void EnableFor(string exePath)
        {
            if (String.IsNullOrEmpty(exePath)) { return; }
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                if (key == null)
                {
                    throw new InvalidOperationException(
                        "无法写入当前用户的启动项（HKCU\\" + RunKey + "）。");
                }
                key.SetValue(ValueName, CommandLine(exePath), RegistryValueKind.String);
            }
        }

        public static void Disable()
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (key != null) { key.DeleteValue(ValueName, false); }
                }
            }
            catch
            {
                // 删除失败不该阻断卸载：文件清理结束后用户仍可在「设置 → 应用 →
                // 启动」里手动关掉（Windows 11 会列出 Run 项）。
            }
        }

        /// 这台机器上本程序的自启项是否已开启（按安装目录判断，不看当前进程）。
        ///
        /// 安装器判断「要不要在覆盖安装后写回自启」用它：覆盖安装的前半程会清掉
        /// 旧值，而此刻当前 exe 是 setup，`IsEnabled()` 永远为 false。
        public static bool IsEnabledForInstall(string installDir)
        {
            string value = ReadValue();
            if (String.IsNullOrEmpty(value)) { return false; }
            if (!String.IsNullOrEmpty(installDir) &&
                value.IndexOf(Path.GetFullPath(installDir).TrimEnd(
                    Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                    StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return true;
            }
            return value.IndexOf(LauncherPaths.LegacyAppExeName,
                StringComparison.OrdinalIgnoreCase) >= 0;
        }

        /// 删掉旧版（workbuddy-manager 时代）留下的自启项。
        ///
        /// 旧值名是 `WorkBuddyManager`，指向旧安装目录里的 `WorkBuddyManager.exe`；
        /// 升级会连那个 exe 一起清掉，留着这个值就是一条「开机报错」的死链。
        public static void RemoveLegacy()
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (key != null) { key.DeleteValue("WorkBuddyManager", false); }
                }
            }
            catch { }
        }

        /// 卸载清理：只删除**指向本安装目录**的值。同一用户可能还装着另一份，
        /// 不能无条件删掉别人的启动项。
        public static void DisableIfOwned(string installDir)
        {
            if (String.IsNullOrEmpty(installDir)) { return; }
            try
            {
                string dir = Path.GetFullPath(installDir).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string value = ReadValue();
                if (String.IsNullOrEmpty(value)) { return; }
                if (value.IndexOf(dir, StringComparison.OrdinalIgnoreCase) < 0) { return; }
                Disable();
            }
            catch { }
        }

        private static string ReadValue()
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKey, false))
                {
                    return key == null ? null : key.GetValue(ValueName) as string;
                }
            }
            catch
            {
                return null;
            }
        }
    }
}
