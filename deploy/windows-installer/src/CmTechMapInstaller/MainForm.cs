using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;

namespace CmTechMapInstaller;

public sealed class MainForm : Form
{
    private const string RemoteBootstrapUrl = "https://raw.githubusercontent.com/firewaredigital/cmtechmap-workspace/main/applications/deploy/all-in-one/zero-touch-bootstrap.ps1";

    private readonly TextBox _repoUrlText = new();
    private readonly TextBox _branchText = new();
    private readonly TextBox _frontendUrlText = new();
    private readonly TextBox _tunnelPublicUrlText = new();
    private readonly TextBox _tunnelTokenText = new();
    private readonly TextBox _tunnelHostnameText = new();
    private readonly TextBox _workspaceDirText = new();
    private readonly CheckBox _withTunnelCheck = new();
    private readonly CheckBox _quickTunnelCheck = new();
    private readonly CheckBox _skipSmokeCheck = new();
    private readonly CheckBox _skipFrontendPatchCheck = new();
    private readonly Button _installButton = new();
    private readonly Button _cancelButton = new();
    private readonly ProgressBar _progressBar = new();
    private readonly Label _statusLabel = new();
    private readonly RichTextBox _logOutput = new();
    private readonly Label _preflightLabel = new();
    private readonly StringBuilder _sessionLog = new();

    private Process? _currentProcess;
    private InstallerSettings _settings = new();

    public MainForm()
    {
        Text = "CM TechMap Installer";
        Width = 980;
        Height = 760;
        MinimumSize = new Size(900, 680);
        BackColor = Color.FromArgb(12, 18, 28);
        ForeColor = Color.FromArgb(233, 240, 249);
        Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point);
        StartPosition = FormStartPosition.CenterScreen;

        BuildLayout();
        ApplyDefaults();
        LoadSettingsIntoUi();
        EvaluatePreflight();
    }

    private void BuildLayout()
    {
        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(18),
            ColumnCount = 1,
            RowCount = 4,
            BackColor = BackColor,
        };
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

        var titleLabel = new Label
        {
            Text = "CM TechMap Zero-Touch Installer",
            AutoSize = true,
            Font = new Font("Segoe UI Semibold", 19F, FontStyle.Bold, GraphicsUnit.Point),
            Margin = new Padding(0, 0, 0, 8),
        };

        var subtitleLabel = new Label
        {
            Text = "Instala e sobe backend completo no Windows usando WSL + Docker automaticamente.",
            AutoSize = true,
            Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point),
            ForeColor = Color.FromArgb(170, 187, 207),
            Margin = new Padding(0, 0, 0, 16),
        };

        var headerPanel = new Panel { Dock = DockStyle.Top, Height = 76 };
        headerPanel.Controls.Add(titleLabel);
        headerPanel.Controls.Add(subtitleLabel);
        subtitleLabel.Top = titleLabel.Bottom + 6;

        var fieldsCard = CreateCardPanel();
        fieldsCard.Controls.Add(CreateFieldsLayout());

        var actionCard = CreateCardPanel();
        actionCard.Controls.Add(CreateActionLayout());

        var logCard = CreateCardPanel();
        logCard.Controls.Add(CreateLogLayout());

        root.Controls.Add(headerPanel, 0, 0);
        root.Controls.Add(fieldsCard, 0, 1);
        root.Controls.Add(actionCard, 0, 2);
        root.Controls.Add(logCard, 0, 3);

        Controls.Add(root);
    }

    private static Panel CreateCardPanel()
    {
        return new Panel
        {
            Dock = DockStyle.Top,
            Height = 1,
            Padding = new Padding(16),
            Margin = new Padding(0, 0, 0, 14),
            BackColor = Color.FromArgb(22, 30, 43),
            BorderStyle = BorderStyle.FixedSingle,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
        };
    }

    private Control CreateFieldsLayout()
    {
        var grid = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            ColumnCount = 2,
            RowCount = 9,
            AutoSize = true,
        };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 250));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));

        AddLabeledField(grid, 0, "Repository URL", _repoUrlText);
        AddLabeledField(grid, 1, "Branch", _branchText);
        AddLabeledField(grid, 2, "Frontend URL", _frontendUrlText);
        AddLabeledField(grid, 3, "Tunnel Public URL (optional)", _tunnelPublicUrlText);
        AddLabeledField(grid, 4, "Workspace (WSL)", _workspaceDirText);
        AddLabeledField(grid, 5, "Tunnel Token (optional)", _tunnelTokenText, mask: true);
        AddLabeledField(grid, 6, "Tunnel Hostname (optional)", _tunnelHostnameText);

        _withTunnelCheck.Text = "Enable tunnel";
        _withTunnelCheck.AutoSize = true;
        _withTunnelCheck.CheckedChanged += (_, _) =>
        {
            _quickTunnelCheck.Enabled = _withTunnelCheck.Checked;
            _tunnelTokenText.Enabled = _withTunnelCheck.Checked;
            _tunnelHostnameText.Enabled = _withTunnelCheck.Checked;
            _tunnelPublicUrlText.Enabled = _withTunnelCheck.Checked;
            EvaluatePreflight();
        };

        _quickTunnelCheck.Text = "Use quick tunnel mode";
        _quickTunnelCheck.AutoSize = true;
        _quickTunnelCheck.CheckedChanged += (_, _) => EvaluatePreflight();

        _skipSmokeCheck.Text = "Skip smoke checks";
        _skipSmokeCheck.AutoSize = true;

        _skipFrontendPatchCheck.Text = "Skip frontend rewrite patch";
        _skipFrontendPatchCheck.AutoSize = true;

        var optionsPanel = new FlowLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = true,
            Padding = new Padding(0, 8, 0, 0),
        };
        optionsPanel.Controls.Add(_withTunnelCheck);
        optionsPanel.Controls.Add(_quickTunnelCheck);
        optionsPanel.Controls.Add(_skipSmokeCheck);
        optionsPanel.Controls.Add(_skipFrontendPatchCheck);

        grid.Controls.Add(optionsPanel, 1, 7);

        _preflightLabel.AutoSize = true;
        _preflightLabel.ForeColor = Color.FromArgb(255, 197, 102);
        _preflightLabel.Margin = new Padding(0, 8, 0, 0);
        grid.Controls.Add(_preflightLabel, 1, 8);

        return grid;
    }

    private void AddLabeledField(TableLayoutPanel grid, int row, string label, TextBox textBox, bool mask = false)
    {
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var lbl = new Label
        {
            Text = label,
            AutoSize = true,
            Margin = new Padding(0, 8, 8, 8),
            ForeColor = Color.FromArgb(188, 205, 227),
            Font = new Font("Segoe UI", 9.5F, FontStyle.Regular, GraphicsUnit.Point),
        };

        textBox.Dock = DockStyle.Top;
        textBox.Margin = new Padding(0, 5, 0, 5);
        textBox.BorderStyle = BorderStyle.FixedSingle;
        textBox.BackColor = Color.FromArgb(14, 21, 31);
        textBox.ForeColor = Color.FromArgb(234, 243, 255);
        textBox.Height = 30;
        textBox.TextChanged += (_, _) => EvaluatePreflight();
        if (mask)
        {
            textBox.UseSystemPasswordChar = true;
        }

        grid.Controls.Add(lbl, 0, row);
        grid.Controls.Add(textBox, 1, row);
    }

    private Control CreateActionLayout()
    {
        var actions = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            ColumnCount = 3,
            RowCount = 2,
            AutoSize = true,
        };
        actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
        actions.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 140));
        actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));

        _installButton.Text = "Install Now";
        _installButton.Width = 160;
        _installButton.Height = 42;
        _installButton.FlatStyle = FlatStyle.Flat;
        _installButton.FlatAppearance.BorderSize = 0;
        _installButton.BackColor = Color.FromArgb(30, 153, 118);
        _installButton.ForeColor = Color.White;
        _installButton.Font = new Font("Segoe UI Semibold", 10F, FontStyle.Bold, GraphicsUnit.Point);
        _installButton.Click += async (_, _) => await RunInstallerAsync();

        _cancelButton.Text = "Cancel";
        _cancelButton.Width = 120;
        _cancelButton.Height = 42;
        _cancelButton.FlatStyle = FlatStyle.Flat;
        _cancelButton.FlatAppearance.BorderColor = Color.FromArgb(64, 85, 111);
        _cancelButton.FlatAppearance.BorderSize = 1;
        _cancelButton.BackColor = Color.FromArgb(28, 36, 51);
        _cancelButton.ForeColor = Color.FromArgb(217, 230, 246);
        _cancelButton.Enabled = false;
        _cancelButton.Click += (_, _) => CancelInstaller();

        _statusLabel.Text = "Ready";
        _statusLabel.AutoSize = true;
        _statusLabel.ForeColor = Color.FromArgb(159, 176, 199);
        _statusLabel.Padding = new Padding(0, 12, 0, 0);

        _progressBar.Dock = DockStyle.Top;
        _progressBar.Style = ProgressBarStyle.Continuous;
        _progressBar.Height = 22;
        _progressBar.Margin = new Padding(0, 14, 0, 0);

        actions.Controls.Add(_installButton, 0, 0);
        actions.Controls.Add(_cancelButton, 1, 0);
        actions.Controls.Add(_statusLabel, 2, 0);
        actions.Controls.Add(_progressBar, 0, 1);
        actions.SetColumnSpan(_progressBar, 3);

        return actions;
    }

    private Control CreateLogLayout()
    {
        var panel = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 2,
        };
        panel.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        panel.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));

        var label = new Label
        {
            Text = "Installer log",
            AutoSize = true,
            Font = new Font("Segoe UI Semibold", 11F, FontStyle.Bold, GraphicsUnit.Point),
            Margin = new Padding(0, 0, 0, 10),
        };

        _logOutput.Dock = DockStyle.Fill;
        _logOutput.ReadOnly = true;
        _logOutput.BackColor = Color.FromArgb(10, 15, 24);
        _logOutput.ForeColor = Color.FromArgb(175, 230, 206);
        _logOutput.BorderStyle = BorderStyle.FixedSingle;
        _logOutput.Font = new Font("Consolas", 10F, FontStyle.Regular, GraphicsUnit.Point);

        panel.Controls.Add(label, 0, 0);
        panel.Controls.Add(_logOutput, 0, 1);

        return panel;
    }

    private void ApplyDefaults()
    {
        _repoUrlText.Text = "https://github.com/firewaredigital/cmtechmap-workspace.git";
        _branchText.Text = "main";
        _workspaceDirText.Text = "$HOME/cmtechmap-workspace";
        _withTunnelCheck.Checked = true;
        _quickTunnelCheck.Checked = true;
        _skipSmokeCheck.Checked = false;
        _skipFrontendPatchCheck.Checked = false;
        _quickTunnelCheck.Enabled = true;
        _tunnelPublicUrlText.Enabled = true;
    }

    private void LoadSettingsIntoUi()
    {
        _settings = InstallerSettings.Load();

        _repoUrlText.Text = _settings.RepoUrl;
        _branchText.Text = _settings.Branch;
        _frontendUrlText.Text = _settings.FrontendUrl;
        _workspaceDirText.Text = _settings.WorkspaceDir;
        _withTunnelCheck.Checked = _settings.WithTunnel;
        _quickTunnelCheck.Checked = _settings.QuickTunnel;
        _tunnelTokenText.Text = _settings.TunnelToken;
        _tunnelHostnameText.Text = _settings.TunnelHostname;
        _tunnelPublicUrlText.Text = _settings.TunnelPublicUrl;
        _skipSmokeCheck.Checked = _settings.SkipSmoke;
        _skipFrontendPatchCheck.Checked = _settings.SkipFrontendPatch;
    }

    private void SaveSettingsFromUi()
    {
        _settings.RepoUrl = _repoUrlText.Text.Trim();
        _settings.Branch = _branchText.Text.Trim();
        _settings.FrontendUrl = _frontendUrlText.Text.Trim();
        _settings.WorkspaceDir = _workspaceDirText.Text.Trim();
        _settings.WithTunnel = _withTunnelCheck.Checked;
        _settings.QuickTunnel = _quickTunnelCheck.Checked;
        _settings.TunnelToken = _tunnelTokenText.Text.Trim();
        _settings.TunnelHostname = _tunnelHostnameText.Text.Trim();
        _settings.TunnelPublicUrl = _tunnelPublicUrlText.Text.Trim();
        _settings.SkipSmoke = _skipSmokeCheck.Checked;
        _settings.SkipFrontendPatch = _skipFrontendPatchCheck.Checked;
        _settings.Save();
    }

    private void EvaluatePreflight()
    {
        var warnings = new List<string>();

        if (!Uri.TryCreate(_repoUrlText.Text.Trim(), UriKind.Absolute, out _))
        {
            warnings.Add("Repository URL invalid");
        }

        if (!string.IsNullOrWhiteSpace(_frontendUrlText.Text) && !Uri.TryCreate(_frontendUrlText.Text.Trim(), UriKind.Absolute, out _))
        {
            warnings.Add("Frontend URL invalid");
        }

        if (_withTunnelCheck.Checked && !_quickTunnelCheck.Checked && string.IsNullOrWhiteSpace(_tunnelTokenText.Text))
        {
            warnings.Add("Token tunnel selected but token is empty");
        }

        if (!string.IsNullOrWhiteSpace(_tunnelHostnameText.Text) && !Regex.IsMatch(_tunnelHostnameText.Text.Trim(), "^[a-zA-Z0-9.-]+$"))
        {
            warnings.Add("Tunnel hostname invalid");
        }

        if (warnings.Count == 0)
        {
            _preflightLabel.Text = "Preflight: ready";
            _preflightLabel.ForeColor = Color.FromArgb(124, 217, 153);
            _installButton.Enabled = _currentProcess is null;
        }
        else
        {
            _preflightLabel.Text = "Preflight: " + string.Join(" | ", warnings);
            _preflightLabel.ForeColor = Color.FromArgb(255, 197, 102);
            _installButton.Enabled = false;
        }
    }

    private async Task RunInstallerAsync()
    {
        if (_currentProcess is not null)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(_repoUrlText.Text))
        {
            MessageBox.Show("Repository URL is required.", "Validation", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        if (string.IsNullOrWhiteSpace(_branchText.Text))
        {
            MessageBox.Show("Branch is required.", "Validation", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        SaveSettingsFromUi();

        _installButton.Enabled = false;
        _cancelButton.Enabled = true;
        _progressBar.Style = ProgressBarStyle.Marquee;
        _statusLabel.Text = "Installing...";
        AppendLog("Starting installer...\n");

        var psi = BuildInstallerProcessStartInfo();

        _currentProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
        _currentProcess.OutputDataReceived += (_, e) =>
        {
            if (!string.IsNullOrWhiteSpace(e.Data))
            {
                BeginInvoke(() => AppendLog(e.Data + Environment.NewLine));
            }
        };
        _currentProcess.ErrorDataReceived += (_, e) =>
        {
            if (!string.IsNullOrWhiteSpace(e.Data))
            {
                BeginInvoke(() => AppendLog("[err] " + e.Data + Environment.NewLine));
            }
        };

        _currentProcess.Start();
        _currentProcess.BeginOutputReadLine();
        _currentProcess.BeginErrorReadLine();

        await _currentProcess.WaitForExitAsync();

        var exitCode = _currentProcess.ExitCode;
        _currentProcess.Dispose();
        _currentProcess = null;

        _cancelButton.Enabled = false;
        _installButton.Enabled = true;
        _progressBar.Style = ProgressBarStyle.Continuous;
        _progressBar.Value = 0;
        EvaluatePreflight();

        if (exitCode == 0)
        {
            _statusLabel.Text = "Install completed";
            _statusLabel.ForeColor = Color.FromArgb(124, 217, 153);
            AppendLog("\nInstall completed successfully.\n");
            MessageBox.Show(
                "Instalação concluída com sucesso.\n\nSeu backend foi inicializado.",
                "CM TechMap Installer",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
        }
        else
        {
            _statusLabel.Text = "Install failed (see details below)";
            _statusLabel.ForeColor = Color.FromArgb(255, 140, 140);
            AppendLog($"\nInstall failed with exit code {exitCode}.\n");
            ShowFailureDialog(exitCode);
        }
    }

    private void ShowFailureDialog(int exitCode)
    {
        using var dialog = new Form
        {
            Text = "CM TechMap Installer - Falha",
            Width = 860,
            Height = 560,
            StartPosition = FormStartPosition.CenterParent,
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false,
            MinimizeBox = false,
            BackColor = Color.FromArgb(18, 24, 35),
            ForeColor = Color.FromArgb(230, 238, 248),
            Font = new Font("Segoe UI", 10F, FontStyle.Regular, GraphicsUnit.Point),
        };

        var root = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(14),
            ColumnCount = 1,
            RowCount = 4,
        };
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var title = new Label
        {
            Text = $"A instalação falhou (exit code {exitCode}).",
            AutoSize = true,
            Font = new Font("Segoe UI Semibold", 12F, FontStyle.Bold, GraphicsUnit.Point),
            ForeColor = Color.FromArgb(255, 166, 166),
            Margin = new Padding(0, 0, 0, 6),
        };

        var subtitle = new Label
        {
            Text = "Logs recentes abaixo para diagnóstico imediato:",
            AutoSize = true,
            ForeColor = Color.FromArgb(186, 202, 224),
            Margin = new Padding(0, 0, 0, 8),
        };

        var logDetails = new RichTextBox
        {
            Dock = DockStyle.Fill,
            ReadOnly = true,
            BackColor = Color.FromArgb(10, 15, 24),
            ForeColor = Color.FromArgb(184, 229, 205),
            BorderStyle = BorderStyle.FixedSingle,
            Font = new Font("Consolas", 9.5F, FontStyle.Regular, GraphicsUnit.Point),
            Text = GetRecentLog(maxLines: 80),
        };

        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
            WrapContents = false,
            Padding = new Padding(0, 10, 0, 0),
        };

        var okButton = new Button
        {
            Text = "OK",
            Width = 110,
            Height = 34,
            DialogResult = DialogResult.OK,
        };

        var copyButton = new Button
        {
            Text = "Copiar logs",
            Width = 130,
            Height = 34,
        };
        copyButton.Click += (_, _) =>
        {
            try
            {
                Clipboard.SetText(logDetails.Text);
            }
            catch
            {
                // Keep dialog responsive even if clipboard is unavailable.
            }
        };

        buttons.Controls.Add(okButton);
        buttons.Controls.Add(copyButton);

        root.Controls.Add(title, 0, 0);
        root.Controls.Add(subtitle, 0, 1);
        root.Controls.Add(logDetails, 0, 2);
        root.Controls.Add(buttons, 0, 3);

        dialog.Controls.Add(root);
        dialog.AcceptButton = okButton;
        dialog.ShowDialog(this);
    }

    private string GetRecentLog(int maxLines)
    {
        var text = _sessionLog.ToString();
        if (string.IsNullOrWhiteSpace(text))
        {
            return "Sem linhas de log capturadas ate o momento.";
        }

        var normalized = text.Replace("\r\n", "\n");
        var lines = normalized.Split('\n', StringSplitOptions.None);
        var start = Math.Max(0, lines.Length - maxLines);
        var recent = string.Join(Environment.NewLine, lines[start..]);
        return string.IsNullOrWhiteSpace(recent) ? text : recent;
    }

    private ProcessStartInfo BuildInstallerProcessStartInfo()
    {
        var localScript = Path.Combine(AppContext.BaseDirectory, "bootstrap", "zero-touch-bootstrap.ps1");
        var args = BuildBootstrapParameterArguments();

        if (File.Exists(localScript))
        {
            AppendLog($"Using bundled bootstrap script: {localScript}{Environment.NewLine}");
            return new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = $"-NoProfile -ExecutionPolicy Bypass -File \"{localScript}\" {args}",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
        }

        AppendLog("Bundled bootstrap script not found. Falling back to remote script.\n");
        var command = BuildRemoteBootstrapCommand(RemoteBootstrapUrl, args);

        return new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = $"-NoProfile -ExecutionPolicy Bypass -Command \"{command}\"",
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
    }

    private string BuildBootstrapParameterArguments()
    {
        static string PsEscape(string value)
        {
            return value.Replace("'", "''");
        }

        var args = new List<string>
        {
            $"-RepoUrl '{PsEscape(_repoUrlText.Text.Trim())}'",
            $"-Branch '{PsEscape(_branchText.Text.Trim())}'"
        };

        var frontend = _frontendUrlText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(frontend))
        {
            args.Add($"-FrontendUrl '{PsEscape(frontend)}'");
        }

        var workspaceDir = _workspaceDirText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(workspaceDir) && !workspaceDir.Equals("$HOME/cmtechmap-workspace", StringComparison.Ordinal))
        {
            args.Add($"-WorkspaceDir '{PsEscape(workspaceDir)}'");
        }

        if (!_withTunnelCheck.Checked)
        {
            args.Add("-WithoutTunnel");
        }
        else if (_quickTunnelCheck.Checked)
        {
            args.Add("-QuickTunnel");
        }

        var token = _tunnelTokenText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(token))
        {
            args.Add($"-TunnelToken '{PsEscape(token)}'");
        }

        var hostname = _tunnelHostnameText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(hostname))
        {
            args.Add($"-TunnelHostname '{PsEscape(hostname)}'");
        }

        var tunnelPublicUrl = _tunnelPublicUrlText.Text.Trim();
        if (!string.IsNullOrWhiteSpace(tunnelPublicUrl))
        {
            args.Add($"-TunnelPublicUrl '{PsEscape(tunnelPublicUrl)}'");
        }

        if (_skipSmokeCheck.Checked)
        {
            args.Add("-SkipSmoke");
        }

        if (_skipFrontendPatchCheck.Checked)
        {
            args.Add("-SkipFrontendPatch");
        }

        return string.Join(" ", args);
    }

    private static string BuildRemoteBootstrapCommand(string scriptUrl, string bootstrapArgs)
    {
        var inline =
            "$script = (Invoke-WebRequest -UseBasicParsing '" + scriptUrl + "').Content; " +
            "&([scriptblock]::Create($script)) " + bootstrapArgs;

        return inline.Replace("\"", "\\\"");
    }

    private void CancelInstaller()
    {
        try
        {
            if (_currentProcess is { HasExited: false })
            {
                _currentProcess.Kill(entireProcessTree: true);
                AppendLog("Installation canceled by user.\n");
            }
        }
        catch
        {
            AppendLog("Could not terminate installer process cleanly.\n");
        }
        finally
        {
            EvaluatePreflight();
        }
    }

    private void AppendLog(string line)
    {
        _sessionLog.Append(line);
        _logOutput.AppendText(line);
        _logOutput.SelectionStart = _logOutput.TextLength;
        _logOutput.ScrollToCaret();
    }
}
