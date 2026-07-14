using System.Text.Json;

namespace CmTechMapInstaller;

public sealed class InstallerSettings
{
    public string RepoUrl { get; set; } = "https://github.com/firewaredigital/cmtechmap-workspace.git";
    public string Branch { get; set; } = "main";
    public string FrontendUrl { get; set; } = string.Empty;
    public string WorkspaceDir { get; set; } = "$HOME/cmtechmap-workspace";
    public bool WithTunnel { get; set; } = true;
    public bool QuickTunnel { get; set; } = true;
    public string TunnelToken { get; set; } = string.Empty;
    public string TunnelHostname { get; set; } = string.Empty;
    public string TunnelPublicUrl { get; set; } = string.Empty;
    public bool SkipSmoke { get; set; }
    public bool SkipFrontendPatch { get; set; }
    public bool CreateInitialUser { get; set; }
    public string InitialUserName { get; set; } = string.Empty;
    public string InitialUserEmail { get; set; } = string.Empty;
    public string InitialUserUsername { get; set; } = string.Empty;
    public string InitialUserPassword { get; set; } = string.Empty;
    public bool InitialUserAdmin { get; set; }

    public static string SettingsDirectory
    {
        get
        {
            var appData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return Path.Combine(appData, "CmTechMapInstaller");
        }
    }

    public static string SettingsFilePath => Path.Combine(SettingsDirectory, "settings.json");

    public static InstallerSettings Load()
    {
        try
        {
            if (!File.Exists(SettingsFilePath))
            {
                return new InstallerSettings();
            }

            var json = File.ReadAllText(SettingsFilePath);
            var settings = JsonSerializer.Deserialize<InstallerSettings>(json);
            return settings ?? new InstallerSettings();
        }
        catch
        {
            return new InstallerSettings();
        }
    }

    public void Save()
    {
        Directory.CreateDirectory(SettingsDirectory);
        var json = JsonSerializer.Serialize(this, new JsonSerializerOptions
        {
            WriteIndented = true,
        });
        File.WriteAllText(SettingsFilePath, json);
    }
}
