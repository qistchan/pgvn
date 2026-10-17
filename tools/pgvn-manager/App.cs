using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Principal;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;
using System.Windows.Media;
using Microsoft.Win32;

[assembly: AssemblyTitle("PgVN Manager")]
[assembly: AssemblyDescription("Windows configuration and maintenance console for PgVN")]
[assembly: AssemblyProduct("PgVN Manager")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

namespace PgVN.Manager
{
    public sealed class ViewState : INotifyPropertyChanged
    {
        public event PropertyChangedEventHandler PropertyChanged;
        private bool busy;
        private string status = "请选择运行包目录并加载配置。";
        private string log = "";
        public bool Busy { get { return busy; } set { busy = value; Changed("Busy", "Available", "ProgressVisibility"); } }
        public bool Available { get { return !busy; } }
        public Visibility ProgressVisibility { get { return busy ? Visibility.Visible : Visibility.Collapsed; } }
        public string Status { get { return status; } set { status = value; Changed("Status"); } }
        public Brush StatusBrush { get; set; }
        public string LogText { get { return log; } set { log = value; Changed("LogText"); } }
        public string AdminLabel { get; set; }
        public string PackageLabel { get; set; }
        public string DatabaseLabel { get; set; }
        public string ServicesLabel { get; set; }
        public string EndpointLabel { get; set; }
        public string RepositoryLabel { get; set; }
        public void Changed(params string[] names)
        {
            foreach (string name in names) { var handler = PropertyChanged; if (handler != null) handler(this, new PropertyChangedEventArgs(name)); }
        }
        public void Append(string text)
        {
            string next = log + DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "  " + text + Environment.NewLine;
            if (next.Length > 200000) next = next.Substring(next.Length - 200000);
            LogText = next;
        }
    }

    public sealed class RepositoryRow
    {
        public string Name { get; set; }
        public string Head { get; set; }
        public string Generation { get; set; }
        public string Enabled { get; set; }
    }

    public sealed class Desktop
    {
        readonly Window window;
        readonly ViewState view;
        readonly BackendClient backend;
        readonly bool administrator;
        string loadedRoot = "";
        string monitorUrl = "";
        string authzRepository = "";
        string authzGeneration = "";
        bool configured;
        public Desktop(string initialRoot)
        {
            using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("PgVN.Manager.MainWindow.xaml"))
            { window = (Window)XamlReader.Load(resource); }
            administrator = new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
            view = new ViewState { AdminLabel = administrator ? "管理员会话 · 已就绪" : "普通会话 · 可加载运行包", PackageLabel = "尚未加载", DatabaseLabel = "尚未配置", ServicesLabel = "尚未加载", EndpointLabel = "加载配置后显示连接地址。", RepositoryLabel = "当前仓库：demo", StatusBrush = Brushes.SlateGray };
            window.DataContext = view;
            backend = new BackendClient(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Invoke-PgVNManager.ps1"));
            Box("RootPath").Text = initialRoot;
            if (administrator) Find<Button>("ElevateButton").Visibility = Visibility.Collapsed;
            window.AddHandler(Button.ClickEvent, new RoutedEventHandler(OnButton));
            Find<ListBox>("Navigation").SelectionChanged += delegate { Find<TabControl>("Pages").SelectedIndex = Find<ListBox>("Navigation").SelectedIndex; };
            Find<DataGrid>("RepositoryList").SelectionChanged += delegate {
                var item = Find<DataGrid>("RepositoryList").SelectedItem as RepositoryRow;
                if (item != null) Box("Repository").Text = item.Name;
            };
            Box("Repository").TextChanged += delegate { authzRepository = ""; authzGeneration = ""; view.RepositoryLabel = "当前仓库：" + Box("Repository").Text; view.Changed("RepositoryLabel"); };
            Box("RootPath").TextChanged += delegate {
                if (!String.Equals(Box("RootPath").Text.Trim(), loadedRoot, StringComparison.OrdinalIgnoreCase)) {
                    configured = false; monitorUrl = ""; authzRepository = ""; authzGeneration = ""; view.PackageLabel = "目录已更改，请加载"; view.DatabaseLabel = "待重新加载"; view.ServicesLabel = "待重新加载";
                    view.Changed("PackageLabel", "DatabaseLabel", "ServicesLabel");
                }
            };
            window.Closing += delegate(object sender, CancelEventArgs args) {
                if (view.Busy) { args.Cancel = true; SetStatus("操作仍在进行，请等待结果后再关闭窗口。", true); }
            };
            Find<TextBox>("LogView").TextChanged += delegate { Find<TextBox>("LogView").ScrollToEnd(); };
        }
        public void Show() { window.Show(); }
        T Find<T>(string name) where T : class { return (T)window.FindName(name); }
        TextBox Box(string name) { return Find<TextBox>(name); }
        static string Text(IDictionary<string, object> values, string key)
        { object result; return values.TryGetValue(key, out result) && result != null ? Convert.ToString(result, CultureInfo.InvariantCulture) : ""; }
        static bool Flag(IDictionary<string, object> values, string key) { return String.Equals(Text(values, key), "True", StringComparison.OrdinalIgnoreCase); }
        void SetStatus(string message, bool error)
        {
            view.StatusBrush = error ? Brushes.Firebrick : Brushes.SlateGray;
            view.Status = message.Length > 250 ? message.Substring(0, 250) + "…（详见日志）" : message;
            view.Changed("StatusBrush");
        }
        void Navigate(int index) { Find<ListBox>("Navigation").SelectedIndex = index; }
        bool Confirm(string message) { return MessageBox.Show(window, message, "核对操作目标", MessageBoxButton.OKCancel, MessageBoxImage.Information, MessageBoxResult.Cancel) == MessageBoxResult.OK; }
        Dictionary<string, object> Request(string action)
        {
            string root = Box("RootPath").Text.Trim();
            if (String.IsNullOrWhiteSpace(root) || !Path.IsPathRooted(root)) throw new InvalidOperationException("请先选择完整运行包的绝对路径。");
            if (action != "inspect" && (loadedRoot == "" || !String.Equals(Path.GetFullPath(root).TrimEnd('\\'), loadedRoot, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidOperationException("运行目录已更改。请先点击「加载配置」，核对当前目标。");
            if (action != "inspect" && action != "connect" && !administrator) throw new InvalidOperationException("此操作需要管理员权限。请回到概览，以管理员身份重新打开。");
            var sslItem = Find<ComboBox>("SslMode").SelectedItem as ComboBoxItem;
            return new Dictionary<string, object> {
                {"action", action}, {"root", root}, {"host", Box("DbHost").Text.Trim()}, {"dbPort", Box("DbPort").Text.Trim()},
                {"database", Box("Database").Text.Trim()}, {"rolePrefix", Box("RolePrefix").Text.Trim()}, {"node", Box("NodeId").Text.Trim()},
                {"svnPort", Box("SvnPort").Text.Trim()}, {"monitorPort", Box("MonitorPort").Text.Trim()}, {"httpPort", Box("HttpPort").Text.Trim()},
                {"sslMode", sslItem == null ? "disable" : Convert.ToString(sslItem.Content)}, {"caFile", Box("CaFile").Text.Trim()},
                {"serviceName", Box("ServiceName").Text.Trim()}, {"davServiceName", Box("DavServiceName").Text.Trim()}, {"withDav", Find<CheckBox>("WithDav").IsChecked == true},
                {"repository", Box("Repository").Text.Trim()}, {"route", Box("Route").Text.Trim()}
            };
        }
        void OnButton(object sender, RoutedEventArgs args)
        {
            var button = args.OriginalSource as Button;
            if (button == null || !(button.Tag is string) || view.Busy) return;
            string action = (string)button.Tag;
            try {
                if (action.StartsWith("nav.", StringComparison.Ordinal)) { Navigate(Int32.Parse(action.Substring(4), CultureInfo.InvariantCulture)); return; }
                if (action == "pick.root") { string path = PickFolder("选择 PgVN 完整运行包根目录"); if (path != null) Box("RootPath").Text = path; return; }
                if (action == "pick.ca") { var picker = new OpenFileDialog { Filter = "证书文件|*.pem;*.crt;*.cer|所有文件|*.*" }; if (picker.ShowDialog(window) == true) Box("CaFile").Text = picker.FileName; return; }
                if (action == "elevate") { Elevate(); return; }
                if (action == "clear.logs") { view.LogText = ""; return; }
                if (action == "export.logs") { var picker = new SaveFileDialog { FileName = "pgvn-manager-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + ".log", Filter = "日志|*.log" }; if (picker.ShowDialog(window) == true) File.WriteAllText(picker.FileName, view.LogText, new UTF8Encoding(false)); return; }
                if (action == "monitor") {
                    Uri uri;
                    if (!Uri.TryCreate(monitorUrl, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1") throw new InvalidOperationException("先加载有效部署配置。");
                    Process.Start(new ProcessStartInfo(new Uri(uri, "monitor/").AbsoluteUri) { UseShellExecute = true }); return;
                }
                if (action.StartsWith("guide", StringComparison.Ordinal)) { OpenGuide(action == "guide.restore"); return; }
                var request = Request(action);
                if (action == "connect" || action == "provision") { request["dbaUser"] = Box("DbaUser").Text.Trim(); request["dbaPassword"] = Find<PasswordBox>("DbaPassword").Password; Find<PasswordBox>("DbaPassword").Clear(); }
                if (action == "provision" && !Confirm("将连接 " + request["host"] + ":" + request["dbPort"] + "，创建数据库「" + request["database"] + "」及前缀「" + request["rolePrefix"] + "_」的五个角色，并初始化目录：\n\n" + request["root"] + "\n\n已有同名数据库或角色时停止。")) return;
                if (action == "initialize" && !Confirm("使用磁盘上的迁移账号初始化 / 续建数据库「" + request["database"] + "」，并补齐权限与部署密钥。请确认这是专用 PgVN 数据库。")) return;
                if (action == "services.uninstall" && !Confirm("停止并卸载当前目录登记的全部 PgVN 服务。数据库及本地配置将保留。")) return;
                if (action == "services.stop" && !Confirm("停止当前目录的 PgVN 服务，活动客户端连接可能中断。")) return;
                if (action == "services.restart" && !Confirm("重启当前目录的 PgVN 服务，活动客户端连接可能中断。")) return;
                if (action == "repo.disable" && !Confirm("禁用仓库「" + request["repository"] + "」，后续客户端访问会被拒绝。")) return;
                if (action == "authz.publish") {
                    if (authzRepository != Convert.ToString(request["repository"]) || authzGeneration == "") throw new InvalidOperationException("请先读取当前仓库规则，再编辑并发布。");
                    request["authz"] = Box("Authz").Text; request["expectedGeneration"] = authzGeneration;
                    if (!Confirm("用编辑框中的 authz 规则替换仓库「" + request["repository"] + "」的第 " + authzGeneration + " 代规则。请确认用户名和权限。")) return;
                }
                if (action == "user.set") {
                    request["userName"] = Box("UserName").Text.Trim(); request["userPassword"] = Find<PasswordBox>("UserPassword").Password; request["withDav"] = Find<CheckBox>("UserDav").IsChecked == true; Find<PasswordBox>("UserPassword").Clear();
                    if (!Confirm("为用户「" + request["userName"] + "」设置凭据。同名用户的密码会被更新。")) return;
                }
                if (action == "repo.dump") { var picker = new SaveFileDialog { Filter = "SVN dump|*.dump", FileName = Convert.ToString(request["repository"]) + ".dump" }; if (picker.ShowDialog(window) != true) return; request["file"] = picker.FileName; }
                if (action == "repo.load") {
                    var picker = new OpenFileDialog { Filter = "SVN dump|*.dump|所有文件|*.*" }; if (picker.ShowDialog(window) != true) return; request["file"] = picker.FileName;
                    if (!Confirm("把以下文件导入仓库「" + request["repository"] + "」：\n\n" + picker.FileName + "\n\n仅允许 r0 空仓库。程序会先禁用仓库，导入后保持禁用；校验成功后请手动启用。")) return;
                }
                if (action == "backup") {
                    string parent = PickFolder("选择备份父目录（必须在运行目录之外）"); if (parent == null) return;
                    request["file"] = Path.Combine(parent, "PgVN-backup-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 6));
                    if (!Confirm("完整备份当前部署到：\n\n" + request["file"] + "\n\n本目录服务会短暂停止，再恢复到原运行状态。备份含密码和密钥；多节点需另行停止写入。")) return;
                }
                // Run owns its exception boundary and disables all action buttons before
                // its first await. Event handlers never become async void.
                Task operation = Run(action, request);
                GC.KeepAlive(operation);
            } catch (Exception error) { SetStatus(error.Message, true); view.Append("未执行：" + error.Message); }
        }
        async Task Run(string action, Dictionary<string, object> request)
        {
            view.Busy = true; SetStatus("正在执行：" + ActionLabel(action) + "…", false); view.Append("开始：" + ActionLabel(action));
            try {
                var progress = new Progress<string>(delegate(string line) { view.Append(line); SetStatus(line, false); });
                IDictionary<string, object> result = await Task.Run(delegate { return backend.Invoke(request, progress); });
                if (result != null && result.ContainsKey("root")) ApplyState(result);
                if (action == "repo.list") ApplyRepositories(result);
                if ((action == "authz.read" || action == "authz.publish") && result != null) {
                    Box("Authz").Text = Text(result, "authz"); authzRepository = Convert.ToString(request["repository"]); authzGeneration = Text(result, "generation");
                }
                if (action == "logs" && result != null && result.ContainsKey("logs")) {
                    foreach (var item in BackendClient.Records(result["logs"])) { view.Append("--- " + Text(item, "name") + " ---\n" + Text(item, "text")); }
                } else if (result != null && !result.ContainsKey("root")) view.Append(BackendClient.Json().Serialize(result));
                if (action == "repo.create" || action == "repo.enable" || action == "repo.disable" || action == "repo.load") {
                    var refresh = new Dictionary<string, object> { {"action", "repo.list"}, {"root", request["root"]} };
                    try { ApplyRepositories(await Task.Run(delegate { return backend.Invoke(refresh, progress); })); }
                    catch (Exception error) { view.Append("操作成功，但仓库列表刷新失败：" + error.Message); }
                }
                view.Append("完成：" + ActionLabel(action)); SetStatus(ActionLabel(action) + "已完成。", false);
            } catch (Exception error) { view.Append("失败：" + error.Message); SetStatus(error.Message, true); }
            finally { request.Clear(); view.Busy = false; }
        }
        void ApplyState(IDictionary<string, object> state)
        {
            loadedRoot = Text(state, "root"); configured = Flag(state, "configured");
            view.PackageLabel = "摘要校验通过 · " + Text(state, "packageId");
            view.DatabaseLabel = configured ? (Flag(state, "initialized") ? "已初始化（可运行数据库检查）" : "已有配置 · 请检查数据库") : "待初始化";
            var settings = state["settings"] as Dictionary<string, object>;
            if (settings != null) {
                foreach (var field in new[] { new[] {"DbHost","host"},new[] {"DbPort","dbPort"},new[] {"Database","database"},new[] {"RolePrefix","rolePrefix"},new[] {"NodeId","node"},new[] {"SvnPort","svnPort"},new[] {"MonitorPort","monitorPort"},new[] {"HttpPort","httpPort"},new[] {"CaFile","caFile"} }) Box(field[0]).Text = Text(settings, field[1]);
                foreach (ComboBoxItem item in Find<ComboBox>("SslMode").Items) { if (Convert.ToString(item.Content) == Text(settings, "sslMode")) Find<ComboBox>("SslMode").SelectedItem = item; }
                monitorUrl = "http://127.0.0.1:" + Text(settings,"monitorPort") + "/";
                view.EndpointLabel = "SVN      svn://127.0.0.1:" + Text(settings,"svnPort") + "/svn/<仓库>\nDAV      http://127.0.0.1:" + Text(settings,"httpPort") + "/svn/<仓库>\n监控     " + monitorUrl + "monitor/";
            }
            Box("RolePrefix").IsReadOnly = configured;
            Box("ServiceName").Text = Text(state,"serviceName"); Box("DavServiceName").Text = Text(state,"davServiceName"); Find<CheckBox>("WithDav").IsChecked = Flag(state,"withDav");
            var services = BackendClient.Records(state["services"]).ToArray();
            view.ServicesLabel = services.Length == 0 ? "尚未安装" : String.Join(" · ", services.Select(x => Text(x,"name") + ": " + Text(x,"status")));
            view.Changed("PackageLabel", "DatabaseLabel", "ServicesLabel", "EndpointLabel");
        }
        void ApplyRepositories(IDictionary<string, object> result)
        {
            if (result == null || !result.ContainsKey("items")) return;
            var rows = new List<RepositoryRow>();
            foreach (var item in BackendClient.Records(result["items"])) rows.Add(new RepositoryRow { Name = Text(item,"name"), Head = Text(item,"head"), Generation = Text(item,"generation"), Enabled = Flag(item,"enabled") ? "是" : "否" });
            Find<DataGrid>("RepositoryList").ItemsSource = rows;
        }
        string PickFolder(string description)
        {
            using (var dialog = new System.Windows.Forms.FolderBrowserDialog { Description = description, ShowNewFolderButton = true }) {
                return dialog.ShowDialog(new WindowOwner(new System.Windows.Interop.WindowInteropHelper(window).Handle)) == System.Windows.Forms.DialogResult.OK ? dialog.SelectedPath : null;
            }
        }
        sealed class WindowOwner : System.Windows.Forms.IWin32Window { public IntPtr Handle { get; private set; } public WindowOwner(IntPtr handle) { Handle=handle; } }
        void Elevate()
        {
            var start = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location) { UseShellExecute=true, Verb="runas", Arguments="--root " + BackendClient.Quote(Box("RootPath").Text.Trim()) };
            Process.Start(start); window.Close();
        }
        void OpenGuide(bool restore)
        {
            string path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "README.zh-CN.md");
            if (loadedRoot != "") {
                string candidate = Path.Combine(loadedRoot, "deploy", "windows", "README.zh-CN.md");
                if (File.Exists(candidate)) path = candidate;
            }
            if (restore) view.Append("恢复入口：deploy/windows/Restore-PgVN.ps1；独立空数据库、源角色映射、密钥及 DAV 凭据核对步骤见部署指南。GUI 不执行覆盖恢复。");
            if (!File.Exists(path)) throw new FileNotFoundException("未找到随程序附带的部署指南。", path);
            Process.Start(new ProcessStartInfo("notepad.exe", BackendClient.Quote(path)) { UseShellExecute=false });
        }
        static string ActionLabel(string action)
        {
            var labels = new Dictionary<string,string> { {"inspect","加载配置与包校验"},{"connect","测试 DBA 连接"},{"provision","创建数据库并初始化"},{"initialize","继续初始化"},{"configure","保存配置"},{"services.install","安装服务"},{"services.start","启动服务"},{"services.stop","停止服务"},{"services.restart","重启服务"},{"services.uninstall","卸载服务"},{"health","服务健康检查"},{"db.check","数据库结构检查"},{"repo.list","刷新仓库列表"},{"repo.create","创建仓库"},{"repo.enable","启用仓库"},{"repo.disable","禁用仓库"},{"repo.verify","校验仓库历史"},{"repo.dump","导出 SVN 历史"},{"repo.load","导入 SVN 历史"},{"user.set","设置用户凭据"},{"authz.publish","发布访问规则"},{"backup","创建完整备份"},{"logs","读取服务日志"} };
            if (action == "authz.read") return "读取访问规则";
            string label; return labels.TryGetValue(action,out label) ? label : action;
        }
    }
    public static class App
    {
        [STAThread]
        public static int Main(string[] arguments)
        {
            try {
                string root="";
                for (int i=0;i<arguments.Length;i++) { if (arguments[i]=="--root" && i+1<arguments.Length) root=arguments[++i]; }
                if (root=="") { string parent=Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"..")); if (File.Exists(Path.Combine(parent,"package-manifest.json"))) root=parent; }
                var application=new Application { ShutdownMode=ShutdownMode.OnLastWindowClose };
                var desktop=new Desktop(root); desktop.Show(); application.Run(); return 0;
            } catch (Exception error) { MessageBox.Show("PgVN Manager 无法启动：\n" + error.Message,"PgVN Manager",MessageBoxButton.OK,MessageBoxImage.Error); return 1; }
        }
    }
}
