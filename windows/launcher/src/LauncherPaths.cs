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

        /// 按卸载项里记录的 `InstallLocation` 找一处已安装的本程序，
        /// 供安装向导回填安装目录（覆盖安装时 `.env`/账号/`data` 才留得住）。
        public static string FindExistingInstallDir()
        {
            try
            {
                using (RegistryKey key = Registry.CurrentUser.OpenSubKey(UninstallRegKey))
                {
                    if (key == null) { return null; }
                    string installed = key.GetValue("InstallLocation") as string;
                    if (!string.IsNullOrEmpty(installed) && Directory.Exists(installed)
                        && LooksInstalled(installed))
                    {
                        return installed;
                    }
                }
            }
            catch { }
            return null;
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

        public static string SettingsFile
        {
            get { return Path.Combine(AppDataDir, "launcher.conf"); }
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
