"""Read the Windows resource counters of an explicitly supplied child PID."""
import ctypes
from ctypes import wintypes as w


class Memory(ctypes.Structure):
    _fields_=[("cb",w.DWORD),("PageFaultCount",w.DWORD)]+[(name,ctypes.c_size_t) for name in
        ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage","QuotaPagedPoolUsage",
         "QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage","PagefileUsage","PeakPagefileUsage","PrivateUsage")]


def resources(pid):
    kernel=ctypes.WinDLL("kernel32",use_last_error=True); psapi=ctypes.WinDLL("psapi",use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]; kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]; kernel.CloseHandle.restype=w.BOOL
    kernel.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)]; kernel.GetProcessTimes.restype=w.BOOL
    psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,ctypes.POINTER(Memory),w.DWORD]; psapi.GetProcessMemoryInfo.restype=w.BOOL
    handle=kernel.OpenProcess(0x410,False,pid)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    try:
        memory=Memory(); memory.cb=ctypes.sizeof(memory)
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(memory),memory.cb): raise ctypes.WinError(ctypes.get_last_error())
        times=[w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]): raise ctypes.WinError(ctypes.get_last_error())
        cpu=sum((t.dwHighDateTime<<32)|t.dwLowDateTime for t in times[2:])/10000000
        return {"cpu_seconds":cpu,"working_set_bytes":memory.WorkingSetSize,"private_bytes":memory.PrivateUsage}
    finally: kernel.CloseHandle(handle)
