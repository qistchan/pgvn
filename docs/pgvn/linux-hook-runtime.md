# Linux external hook runtime

The Linux `hooks` component builds the POSIX external-hook executor and its
native probe as development artifacts. It is not a complete Linux PgVN product
and does not establish network SVN/DAV acceptance.

External hooks are optional. Built-in hooks and Windows dispatch do not require
a cgroup setting. A Linux node that dispatches external hooks must set a local,
absolute `[pgvn] hook-cgroup-root` to a writable directory delegated to the
PgVN service. The directory must be on unified cgroup v2 and expose
`cgroup.kill` (Linux kernel 5.14 or newer). The executor creates a private
per-dispatch cgroup there and refuses to start the hook if it cannot establish
that boundary. There is no process-group or shell fallback.

For example, a service manager can delegate a dedicated subtree to the service
account:

```ini
[Service]
Delegate=yes
```

Provision a writable cgroup-v2 directory within that delegated subtree and
configure its absolute path:

```ini
[pgvn]
hook-cgroup-root=/sys/fs/cgroup/system.slice/pgvn.service/pgvn-hooks
```

The configured path must already exist. It must not contain symlink path
components. The service account needs permission to create and remove child
cgroups and write `cgroup.procs` and `cgroup.kill`. Do not point this setting at
the global cgroup root or a directory shared with unrelated services.

The executor passes literal UTF-8 arguments (never a shell command), sends the
versioned event JSON on standard input, and uses a deliberate environment
allowlist. It reopens the configured hook root and executable without following
symlinks, pins the executable file descriptor for execution, bounds retained
output, and drains pipes nonblocking. On every outcome it kills the dispatch
cgroup and waits boundedly for it to become empty before returning. If a
cgroup cannot become empty, including because a descendant is stuck in
uninterruptible sleep, the executor returns a cleanup failure diagnosis rather
than waiting indefinitely or claiming hook success. Missing or unusable
containment is a dispatch error, not hook success.

Cgroups here provide process-lifecycle containment for trusted,
operator-installed hook programs. They are not a security sandbox for
malicious hook code. Hook programs execute with the service account's
permissions and must be reviewed and installed by the operator.

The supported component build uses the pinned SDK produced by
`prepare_linux_sdk.py` and the Ubuntu 24.04 Linux builder. Record the builder
image, SDK manifest identity, compiler, and sanitizer configuration with each
build/test run. The development host may differ from the target builder; do not
describe a host-only run as an Ubuntu 24.04 verification. This milestone
delivers only the hook runtime component; the remaining Linux product
milestones and Windows W1/W2 validation remain separate.
