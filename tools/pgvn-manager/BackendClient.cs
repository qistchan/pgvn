using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace PgVN.Manager
{
    public sealed class BackendClient
    {
        readonly string script;
        public BackendClient(string scriptPath) { script=scriptPath; }
        public static JavaScriptSerializer Json() { return new JavaScriptSerializer { MaxJsonLength=2097152, RecursionLimit=30 }; }
        public static IEnumerable<IDictionary<string,object>> Records(object value)
        {
            // JavaScriptSerializer represents untyped JSON arrays as ArrayList
            // when nested in Dictionary<string,object>. Accept either CLR form.
            if (value==null) return Enumerable.Empty<IDictionary<string,object>>();
            var collection=value as System.Collections.IEnumerable;
            if (collection==null || value is string) throw new InvalidDataException("管理后台返回了无效列表。");
            return collection.Cast<object>().Select(item => {
                var record=item as IDictionary<string,object>;
                if (record==null) throw new InvalidDataException("管理后台返回了无效列表条目。");
                return record;
            });
        }
        public static string Quote(string value)
        {
            if (value.Length>0 && value.All(c => !Char.IsWhiteSpace(c) && c!='"')) return value;
            var result=new StringBuilder("\""); int slashes=0;
            foreach (char c in value) { if (c=='\\') { slashes++; continue; } result.Append('\\',c=='"' ? slashes*2+1 : slashes); result.Append(c); slashes=0; }
            result.Append('\\',slashes*2); return result.Append('"').ToString();
        }
        static string Redact(string value, IEnumerable<string> secrets)
        {
            foreach (string secret in secrets) { if (!String.IsNullOrEmpty(secret)) value=value.Replace(secret,"[REDACTED]"); }
            return value;
        }
        public IDictionary<string, object> Invoke(Dictionary<string, object> request, IProgress<string> progress)
        {
            if (!File.Exists(script)) throw new FileNotFoundException("缺少管理后台脚本。请重新解压完整 GUI 包。",script);
            string powershell=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"WindowsPowerShell", "v1.0", "powershell.exe");
            string[] secrets=request.Where(x=>x.Key.IndexOf("password",StringComparison.OrdinalIgnoreCase)>=0).Select(x=>Convert.ToString(x.Value)).ToArray();
            var start=new ProcessStartInfo(powershell,"-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(script) + " -RequestStdIn") {
                UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true,
                StandardOutputEncoding=new UTF8Encoding(false), StandardErrorEncoding=new UTF8Encoding(false)
            };
            // Do not inherit unrelated libpq credentials from the caller's shell.
            foreach (string name in new[] {"PGPASSWORD","PGSERVICE","PGSERVICEFILE","PGPASSFILE","PGHOST","PGPORT","PGUSER","PGDATABASE","PGOPTIONS"}) start.EnvironmentVariables.Remove(name);
            using (var job=new ChildJob())
            using (var process=new Process { StartInfo=start }) {
                if (!process.Start()) throw new InvalidOperationException("无法启动管理后台。");
                try { job.Assign(process); } catch { process.Kill(); throw; }
                Task<string> errors=process.StandardError.ReadToEndAsync();
                byte[] bytes=Encoding.UTF8.GetBytes(Json().Serialize(request));
                try { process.StandardInput.BaseStream.Write(bytes,0,bytes.Length); process.StandardInput.Close(); }
                finally { Array.Clear(bytes,0,bytes.Length); }
                IDictionary<string,object> result=null; string failure=null; bool completed=false;
                string line;
                while ((line=process.StandardOutput.ReadLine())!=null) {
                    if (!line.StartsWith("PGVN-MANAGER/1 ",StringComparison.Ordinal)) { if (progress!=null) progress.Report(Redact(line,secrets)); continue; }
                    var envelope=Json().Deserialize<Dictionary<string,object>>(line.Substring(15));
                    string kind=Convert.ToString(envelope["kind"]);
                    if (kind=="error") failure=Redact(Convert.ToString(envelope["data"]),secrets);
                    else if (kind=="result") { result=envelope["data"] as Dictionary<string,object>; completed=true; }
                    else if (kind=="log" && progress!=null) progress.Report(Redact(Convert.ToString(envelope["data"]),secrets));
                }
                process.WaitForExit();
                string errorText=Redact(errors.GetAwaiter().GetResult(),secrets);
                if (process.ExitCode!=0 || failure!=null || !completed) throw new InvalidOperationException(failure ?? ("管理后台异常退出（" + process.ExitCode + "）。" + errorText));
                return result;
            }
        }
        // A backend waits for stdin before starting work. Assign it first, then
        // send the request so an orphaned child cannot outlive this GUI.
        sealed class ChildJob : IDisposable
        {
            IntPtr handle;
            [StructLayout(LayoutKind.Sequential)] struct Basic { public long PerProcess,PerJob; public uint LimitFlags; public UIntPtr MinWorking,MaxWorking; public uint ActiveProcess; public UIntPtr Affinity; public uint Priority,Scheduling; }
            [StructLayout(LayoutKind.Sequential)] struct Io { public ulong ReadOps,WriteOps,OtherOps,ReadBytes,WriteBytes,OtherBytes; }
            [StructLayout(LayoutKind.Sequential)] struct Extended { public Basic Basic; public Io Io; public UIntPtr ProcessMemory,JobMemory,PeakProcessMemory,PeakJobMemory; }
            [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
            [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int info,ref Extended value,uint length);
            [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
            [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
            public ChildJob()
            {
                handle=CreateJobObject(IntPtr.Zero,null);
                if (handle==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
                var limits=new Extended(); limits.Basic.LimitFlags=0x2000;
                if (!SetInformationJobObject(handle,9,ref limits,(uint)Marshal.SizeOf(typeof(Extended)))) { int code=Marshal.GetLastWin32Error(); Dispose(); throw new System.ComponentModel.Win32Exception(code); }
            }
            public void Assign(Process process) { if (!AssignProcessToJobObject(handle,process.Handle)) throw new System.ComponentModel.Win32Exception(); }
            public void Dispose() { if (handle!=IntPtr.Zero) { CloseHandle(handle); handle=IntPtr.Zero; } }
        }
    }
}
