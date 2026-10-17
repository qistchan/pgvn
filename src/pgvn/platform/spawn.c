#include "platform_internal.h"
#include "../core/hook_name.h"
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

static int ppid_of(pid_t pid, pid_t *out)
{
    char path[64], line[256];
    FILE *file;
    int value = 0;
    snprintf(path, sizeof(path), "/proc/%d/status", (int)pid);
    file = fopen(path, "r");
    if (!file) return -1;
    while (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "PPid: %d", &value) == 1) {
            *out = (pid_t)value;
            fclose(file);
            return 0;
        }
    }
    fclose(file);
    return -1;
}

static int list_children(pid_t *out, int max)
{
    DIR *dir = opendir("/proc");
    struct dirent *entry;
    int count = 0;
    pid_t self = getpid();
    if (!dir) return 0;
    while ((entry = readdir(dir))) {
        char *end = NULL;
        long value = strtol(entry->d_name, &end, 10);
        pid_t parent = 0;
        if (!end || *end || value <= 1) continue;
        if (ppid_of((pid_t)value, &parent) || parent != self) continue;
        if (count < max) out[count++] = (pid_t)value;
    }
    closedir(dir);
    return count;
}

static int known_pid(const pid_t *list, int count, pid_t pid)
{
    int i;
    for (i = 0; i < count; ++i) if (list[i] == pid) return 1;
    return 0;
}

static int unexpected_children(const pid_t *before, int before_count, pid_t child)
{
    pid_t now[128];
    int count = list_children(now, 128), i;
    for (i = 0; i < count; ++i)
        if (now[i] != child && !known_pid(before, before_count, now[i])) return 1;
    return 0;
}

static void reap_pid(pid_t pid)
{
    int spins;
    if (pid <= 1) return;
    for (spins = 0; spins < 50; ++spins) {
        pid_t got = waitpid(pid, NULL, WNOHANG);
        if (got == pid || (got < 0 && errno == ECHILD)) return;
        usleep(10000);
    }
}

static void kill_new_children(const pid_t *before, int before_count, pid_t child)
{
    int spins;
    if (child > 1) {
        kill(-child, SIGKILL);
        kill(child, SIGKILL);
        reap_pid(child);
    }
    for (spins = 0; spins < 20; ++spins) {
        pid_t now[128];
        int count = list_children(now, 128), i, pending = 0;
        for (i = 0; i < count; ++i) {
            if (now[i] == child || known_pid(before, before_count, now[i])) continue;
            pending = 1;
            kill(now[i], SIGKILL);
            reap_pid(now[i]);
        }
        if (!pending) return;
    }
}

static int open_executable(const char *root, const char *name, int *fd)
{
    char path[4096], base[256];
    int parent = -1, file, code, access_ok;
    struct stat info;
    size_t root_len, name_len;
    *fd = -1;
    if (!root || !name || !pgvn_hook_executable_valid(name, 0) || strchr(name, '/')) return PGVN_PLATFORM_INVALID;
    root_len = strlen(root);
    name_len = strlen(name);
    if (!root_len || root_len + 1 + name_len >= sizeof(path)) return PGVN_PLATFORM_INVALID;
    memcpy(path, root, root_len);
    path[root_len] = '/';
    memcpy(path + root_len + 1, name, name_len + 1);
    code = pgvn_open_parent(path, &parent, base, sizeof(base));
    if (code) return code;
    file = openat(parent, base, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (file < 0) {
        code = errno == ENOENT ? PGVN_PLATFORM_NOT_FOUND : PGVN_PLATFORM_DENIED;
        close(parent);
        return code;
    }
    access_ok = faccessat(parent, base, X_OK, AT_EACCESS);
    close(parent);
    if (fstat(file, &info) || !S_ISREG(info.st_mode) || (info.st_mode & 0002) || access_ok) {
        close(file);
        return PGVN_PLATFORM_DENIED;
    }
    *fd = file;
    return PGVN_PLATFORM_OK;
}

static void become_child(int script, int input, int output, int error, char **argv, const char *cgroup)
{
    char *envp[] = {"PATH=/usr/bin:/bin", "LC_ALL=C.UTF-8", "PGVN_HOOK=1", NULL};
    int kept = script, fd;
    if (cgroup && *cgroup) {
        FILE *procs = fopen(cgroup, "w");
        if (procs) {
            fprintf(procs, "%d\n", (int)getpid());
            fclose(procs);
        }
    }
    if (setpgid(0, 0) && errno != EACCES) _exit(127);
    if (script <= 2) {
        kept = dup(script);
        if (kept < 0) _exit(127);
    }
    if (dup2(input, STDIN_FILENO) < 0 || dup2(output, STDOUT_FILENO) < 0 || dup2(error, STDERR_FILENO) < 0) _exit(127);
    for (fd = 3; fd < 256; ++fd) if (fd != kept) close(fd);
    /* Script interpreters reopen the fd via /proc/self/fd; CLOEXEC makes that fail. */
    if (fcntl(kept, F_SETFD, 0)) _exit(127);
    fexecve(kept, argv, envp);
    _exit(127);
}

static int add_bytes(char *buffer, size_t *used, size_t limit, int *truncated, const char *data, ssize_t got)
{
    size_t room, take;
    if (got <= 0) return 0;
    room = *used < limit ? limit - *used : 0;
    take = (size_t)got < room ? (size_t)got : room;
    if (take) memcpy(buffer + *used, data, take);
    *used += take;
    if ((size_t)got > take) *truncated = 1;
    return 0;
}

static int prepare_cgroup(pid_t pid, char *directory, size_t directory_cap, char *procs, size_t procs_cap)
{
    FILE *file;
    char line[512];
    directory[0] = 0;
    procs[0] = 0;
    file = fopen("/proc/self/cgroup", "r");
    if (!file) return -1;
    while (fgets(line, sizeof(line), file)) {
        char *relative, *newline;
        int wrote;
        if (strncmp(line, "0::", 3)) continue;
        relative = line + 3;
        newline = strchr(relative, '\n');
        if (newline) *newline = 0;
        if (!relative[0] || !strcmp(relative, "/")) relative = "";
        wrote = snprintf(directory, directory_cap, "/sys/fs/cgroup%s/pgvn-hook-%d", relative, (int)pid);
        if (wrote < 0 || (size_t)wrote >= directory_cap) break;
        if (mkdir(directory, 0700)) break;
        wrote = snprintf(procs, procs_cap, "%s/cgroup.procs", directory);
        if (wrote > 0 && (size_t)wrote < procs_cap) {
            fclose(file);
            return 0;
        }
        rmdir(directory);
        break;
    }
    fclose(file);
    directory[0] = 0;
    return -1;
}

static void finish_cgroup(const char *directory)
{
    char path[512];
    FILE *file;
    if (!directory || !*directory) return;
    snprintf(path, sizeof(path), "%s/cgroup.kill", directory);
    file = fopen(path, "w");
    if (file) {
        fputs("1\n", file);
        fclose(file);
    }
    rmdir(directory);
}

int pgvn_hook_files_ready(const char *root, const char *const *names, int count)
{
    int i;
    if (!root || count < 0 || (!names && count)) return PGVN_PLATFORM_INVALID;
    for (i = 0; i < count; ++i) {
        int fd = -1, code = open_executable(root, names[i], &fd);
        if (code) return code;
        close(fd);
    }
    return PGVN_PLATFORM_OK;
}

int pgvn_spawn_capture(const pgvn_spawn_request *request, pgvn_spawn_result *result)
{
    char mapped[133], cgroup_dir[512], cgroup_procs[544];
    char *argv[PGVN_HOOK_ARGUMENTS_MAX + 2];
    int script = -1, input[2] = {-1, -1}, output[2] = {-1, -1}, error[2] = {-1, -1};
    int code, argument_count = 0, i, status = 0, exited = 0, stdout_open = 1, stderr_open = 1;
    pid_t before[128], child = -1;
    int before_count;
    size_t limit, stdin_off = 0;
    uint64_t deadline;
    if (!result) return PGVN_PLATFORM_INVALID;
    memset(result, 0, sizeof(*result));
    result->code = PGVN_PLATFORM_INVALID;
    result->exit_code = -1;
    if (!request || request->timeout_seconds < 1 || request->timeout_seconds > 60) return PGVN_PLATFORM_INVALID;
    limit = request->output_limit ? request->output_limit : PGVN_HOOK_OUTPUT_MAX;
    if (limit > PGVN_HOOK_OUTPUT_MAX || request->stdin_len > 1048576 || (request->stdin_len && !request->stdin_data))
        return PGVN_PLATFORM_INVALID;
    if (!pgvn_hook_map_resolve(request->executable, NULL, mapped, sizeof(mapped))) return PGVN_PLATFORM_INVALID;
    if (request->arguments) {
        while (request->arguments[argument_count]) {
            if (argument_count >= PGVN_HOOK_ARGUMENTS_MAX || strlen(request->arguments[argument_count]) > 2048)
                return PGVN_PLATFORM_INVALID;
            ++argument_count;
        }
    }
    argv[0] = mapped;
    for (i = 0; i < argument_count; ++i) argv[i + 1] = (char *)request->arguments[i];
    argv[argument_count + 1] = NULL;
    code = open_executable(request->root, mapped, &script);
    if (code) {
        result->code = code;
        return code;
    }
    if (pipe(input) || pipe(output) || pipe(error)) {
        code = PGVN_PLATFORM_IO;
        goto done;
    }
    for (i = 0; i < 2; ++i) {
        fcntl(input[i], F_SETFD, FD_CLOEXEC);
        fcntl(output[i], F_SETFD, FD_CLOEXEC);
        fcntl(error[i], F_SETFD, FD_CLOEXEC);
    }
    fcntl(input[1], F_SETFL, O_NONBLOCK);
    fcntl(output[0], F_SETFL, O_NONBLOCK);
    fcntl(error[0], F_SETFL, O_NONBLOCK);
    cgroup_dir[0] = 0;
    prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0);
    before_count = list_children(before, 128);
    child = fork();
    if (child < 0) {
        code = PGVN_PLATFORM_IO;
        goto done;
    }
    if (!child) {
        close(input[1]);
        close(output[0]);
        close(error[0]);
        cgroup_procs[0] = 0;
        become_child(script, input[0], output[1], error[1], argv, NULL);
    }
    close(input[0]);
    input[0] = -1;
    close(output[1]);
    output[1] = -1;
    close(error[1]);
    error[1] = -1;
    close(script);
    script = -1;
    setpgid(child, child);
    if (!prepare_cgroup(child, cgroup_dir, sizeof(cgroup_dir), cgroup_procs, sizeof(cgroup_procs))) {
        FILE *procs = fopen(cgroup_procs, "w");
        if (procs) {
            fprintf(procs, "%d\n", (int)child);
            fclose(procs);
        }
    }
    deadline = pgvn_monotonic_ms() + (uint64_t)request->timeout_seconds * 1000u;
    code = PGVN_PLATFORM_OK;
    if (!request->stdin_len && input[1] >= 0) {
        close(input[1]);
        input[1] = -1;
    }
    for (;;) {
        struct pollfd polls[3];
        int poll_count = 0, ready, stdout_index = -1, stderr_index = -1, stdin_index = -1;
        uint64_t now = pgvn_monotonic_ms();
        char chunk[1024];
        if (request->cancel && *request->cancel) { code = PGVN_PLATFORM_CANCELLED; break; }
        if (now >= deadline) { code = PGVN_PLATFORM_TIMEOUT; break; }
        if (!exited) {
            pid_t got = waitpid(child, &status, WNOHANG);
            if (got == child) exited = 1;
            else if (got < 0 && errno != EINTR) { code = PGVN_PLATFORM_IO; break; }
        }
        if (exited && input[1] >= 0) {
            close(input[1]);
            input[1] = -1;
        }
        if (input[1] >= 0) {
            polls[poll_count].fd = input[1];
            polls[poll_count].events = POLLOUT;
            stdin_index = poll_count++;
        }
        if (stdout_open) {
            polls[poll_count].fd = output[0];
            polls[poll_count].events = POLLIN;
            stdout_index = poll_count++;
        }
        if (stderr_open) {
            polls[poll_count].fd = error[0];
            polls[poll_count].events = POLLIN;
            stderr_index = poll_count++;
        }
        if (exited && code == PGVN_PLATFORM_OK && unexpected_children(before, before_count, child)) {
            kill_new_children(before, before_count, child);
            code = PGVN_PLATFORM_DESCENDANT;
            break;
        }
        if (exited && !stdout_open && !stderr_open && input[1] < 0) break;
        if (!poll_count) {
            usleep(10000);
            continue;
        }
        ready = poll(polls, (nfds_t)poll_count, 50);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { code = PGVN_PLATFORM_IO; break; }
        if (stdin_index >= 0 && (polls[stdin_index].revents & (POLLOUT | POLLERR | POLLHUP))) {
            ssize_t wrote = write(input[1], request->stdin_data + stdin_off, request->stdin_len - stdin_off);
            if (wrote > 0) stdin_off += (size_t)wrote;
            else if (wrote < 0 && errno != EAGAIN && errno != EINTR) {
                close(input[1]);
                input[1] = -1;
            }
            if (input[1] >= 0 && stdin_off >= request->stdin_len) {
                close(input[1]);
                input[1] = -1;
            }
        }
        if (stdout_index >= 0 && (polls[stdout_index].revents & (POLLIN | POLLHUP | POLLERR))) {
            ssize_t got = read(output[0], chunk, sizeof(chunk));
            if (got > 0) add_bytes(result->stdout_buf, &result->stdout_len, limit, &result->truncated_stdout, chunk, got);
            else if (got == 0 || (got < 0 && errno != EAGAIN && errno != EINTR)) {
                close(output[0]);
                output[0] = -1;
                stdout_open = 0;
            }
        }
        if (stderr_index >= 0 && (polls[stderr_index].revents & (POLLIN | POLLHUP | POLLERR))) {
            ssize_t got = read(error[0], chunk, sizeof(chunk));
            if (got > 0) add_bytes(result->stderr_buf, &result->stderr_len, limit, &result->truncated_stderr, chunk, got);
            else if (got == 0 || (got < 0 && errno != EAGAIN && errno != EINTR)) {
                close(error[0]);
                error[0] = -1;
                stderr_open = 0;
            }
        }
    }
    if (input[1] >= 0) close(input[1]);
    input[1] = -1;
    if (code == PGVN_PLATFORM_TIMEOUT || code == PGVN_PLATFORM_CANCELLED) {
        kill_new_children(before, before_count, child);
        finish_cgroup(cgroup_dir);
    }
    if (!exited && child > 0) {
        int spins;
        for (spins = 0; spins < 50; ++spins) {
            pid_t got = waitpid(child, &status, WNOHANG);
            if (got == child) { exited = 1; break; }
            usleep(10000);
        }
        if (!exited) {
            kill_new_children(before, before_count, child);
            waitpid(child, &status, 0);
            exited = 1;
        }
    }
    if (exited) {
        if (WIFEXITED(status)) result->exit_code = WEXITSTATUS(status);
        else if (WIFSIGNALED(status)) result->exit_code = 128 + WTERMSIG(status);
    }
    if (code == PGVN_PLATFORM_OK && exited) {
        usleep(20000);
        if (unexpected_children(before, before_count, child)) {
            kill_new_children(before, before_count, child);
            code = PGVN_PLATFORM_DESCENDANT;
        }
    }
    finish_cgroup(cgroup_dir);
    result->stdout_buf[result->stdout_len] = 0;
    result->stderr_buf[result->stderr_len] = 0;
done:
    if (script >= 0) close(script);
    for (i = 0; i < 2; ++i) {
        if (input[i] >= 0) close(input[i]);
        if (output[i] >= 0) close(output[i]);
        if (error[i] >= 0) close(error[i]);
    }
    result->code = code;
    return code;
}
