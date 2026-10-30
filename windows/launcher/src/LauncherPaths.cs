using System;
using System.IO;
using Microsoft.Win32;

namespace TokenFarmLauncher
{
    /// 应用级常量与固定路径。
    internal static class LauncherPaths
    {
        public const string AppName = "token-farm";
        public const string AppExeName = "TokenFarm.exe";
        public const string UninstallExeName = "Uninstall.exe";
        public const string UninstallRegKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\token-farm";

        // ── 旧版（workbuddy-manager 时代的 v2.x）标识 ────────────────────────
        // 只用于迁移与清理：默认安装目录、偏好目录、卸载项都换了名字，老机器上的
        // 那些位置得能认出来，否则升级后会变成「装了两份」，旧数据也不会被接管。
        public const string LegacyAppName = "WorkBuddy Manager";
        public const string LegacyAppExeName = "WorkBuddyManager.exe";
        public const string LegacyUninstallRegKey =
            @"Software\Microsoft\Windows\CurrentVersion\Uninstall\WorkBuddyManager";
        public const string LegacyManifestName = "WorkBuddyManager.install.txt";

        /// 内嵌 payload 的资源名（csc -resource:payload.zip,payload.zip）。
        public const string PayloadResourceName = "payload.zip";

        /// 安装标识：payload 解压产物之一，用它判断目录里是否是一个安装好的项目。
        private const string InstallMarker = @"server\main.py";

        public static bool LooksInstalled(string dir)
        {
            return !string.IsNullOrEmpty(dir) && File.Exists(Path.Combine(dir, InstallMarker));
        }

        public static string DefaultInstallDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "token-farm");
            }
        }

        /// 旧版默认安装目录（`%USERPROFILE%\WorkBuddyManager`）。
        public static string LegacyInstallDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "WorkBuddyManager");
            }
        }

        /// 旧版装过、且还在原处时返回它的安装根；否则 null。
        public static string DetectedLegacyInstallDir
        {
            get { return LooksInstalled(LegacyInstallDir) ? LegacyInstallDir : null; }
        }

        /// 按「新卸载项 → 旧卸载项 → 旧默认目录」的顺序找一处已安装的旧版，
        /// 供安装向导回填安装目录（升级时原地覆盖，`.env`/账号/`data` 才留得住）。
        public static string FindExistingInstallDir()
        {
            string[] regKeys = { UninstallRegKey, LegacyUninstallRegKey };
            foreach (string regKey in regKeys)
            {
                try
                {
                    using (RegistryKey key = Registry.CurrentUser.OpenSubKey(regKey))
                    {
                        if (key == null) { continue; }
                        string installed = key.GetValue("InstallLocation") as string;
                        if (!string.IsNullOrEmpty(installed) && Directory.Exists(installed)
                            && LooksInstalled(installed))
                        {
                            return installed;
                        }
                    }
                }
                catch { }
            }
            return DetectedLegacyInstallDir;
        }

        /// 项目内脚本路径（start/prepare/service-tools 等都在 windows\ 下）。
        public static string ScriptPath(string root, string fileName)
        {
            return Path.Combine(Path.Combine(root, "windows"), fileName);
        }

        public static string EnvFilePath(string root)
        {
            return Path.Combine(root, ".env");
        }

        /// 启动器偏好目录（%APPDATA%\token-farm）。
        public static string AppDataDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "token-farm");
            }
        }

        /// 旧版偏好目录（%APPDATA%\WorkBuddyManager）。
        public static string LegacyAppDataDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "WorkBuddyManager");
            }
        }

        public static string SettingsFile
        {
            get { return Path.Combine(AppDataDir, "launcher.conf"); }
        }

        /// 首次运行新版本时接管旧版的偏好文件（窗口行为、托盘提示等）。
        /// 只在新文件不存在时复制一份，旧文件原样保留 —— 迁移失败也不影响启动。
        public static void MigrateLegacySettings()
        {
            try
            {
                if (File.Exists(SettingsFile)) { return; }
                string legacy = Path.Combine(LegacyAppDataDir, "launcher.conf");
                if (!File.Exists(legacy)) { return; }
                Directory.CreateDirectory(AppDataDir);
                File.Copy(legacy, SettingsFile, false);
            }
            catch { }
        }

        public static string DesktopShortcutPath
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), AppName + ".lnk");
            }
        }

        /// 开始菜单的程序组目录（%APPDATA%\Microsoft\Windows\Start Menu\Programs\token-farm）。
        public static string StartMenuFolder
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), AppName);
            }
        }
    }
}
