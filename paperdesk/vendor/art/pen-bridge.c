/* Streams raw input_event records from a Paper Pro input device to stdout.
 * Usage: pen-bridge g|- [/dev/input/eventN [FIFO]]   (default event2, the marker)
 * `g` additionally takes EVIOCGRAB so xochitl ignores the device while a
 * desktop session uses it. The kernel drops the grab when this process
 * exits, and it exits as soon as stdout (the SSH link) goes away.
 * With FIFO, the same records are also offered, without ever blocking, to a
 * local reader (the viewport library, which inks strokes on the tablet's own
 * screen without waiting for the desktop).
 * Build (see ../README.md): clang --target=aarch64-linux-gnu -nostdlib -static ... */
#include <linux/input.h>

static long call(long n, long a, long b, long c, long d, long e) {
    register long r8 __asm__("x8") = n, r0 __asm__("x0") = a, r1 __asm__("x1") = b;
    register long r2 __asm__("x2") = c, r3 __asm__("x3") = d, r4 __asm__("x4") = e;
    __asm__ volatile("svc #0" : "+r"(r0) : "r"(r8), "r"(r1), "r"(r2), "r"(r3), "r"(r4) : "memory");
    return r0;
}
enum { IOCTL = 29, OPENAT = 56, CLOSE = 57, READ = 63, WRITE = 64, PPOLL = 73, EXIT = 94 };
struct pfd { int fd; short events, revents; };
#define BAD (0x008 | 0x010 | 0x020) /* POLLERR | POLLHUP | POLLNVAL */

__asm__(".global _start\n_start:\n mov x0, sp\n bl run\n");
void run(long *sp) {
    char **argv = (char **)(sp + 1);
    int fd = (int)call(OPENAT, -100, (long)(sp[0] > 2 ? argv[2] : "/dev/input/event2"), 0, 0, 0);
    if (fd < 0) call(EXIT, 2, 0, 0, 0, 0);
    if (sp[0] > 1 && argv[1][0] == 'g' && call(IOCTL, fd, EVIOCGRAB, 1, 0, 0) < 0) call(EXIT, 3, 0, 0, 0, 0);
    static char buf[sizeof(struct input_event) * 64];
    const char *fifo = sp[0] > 3 ? argv[3] : 0;
    int tee = -1; unsigned turn = 0;
    for (;;) {
        struct pfd p[2] = {{fd, 1, 0}, {1, 0, 0}};
        if (call(PPOLL, (long)p, 2, 0, 0, 0) < 0 || (p[1].revents & BAD) || (p[0].revents & BAD)) break;
        if (!(p[0].revents & 1)) continue;
        long n = call(READ, fd, (long)buf, sizeof buf, 0, 0), done = 0;
        if (n <= 0) break;
        if (fifo && tee < 0 && (turn++ & 63) == 0) tee = (int)call(OPENAT, -100, (long)fifo, 0x801, 0, 0); /* O_WRONLY|O_NONBLOCK */
        if (tee >= 0 && call(WRITE, tee, (long)buf, n, 0, 0) == -32) { call(CLOSE, tee, 0, 0, 0, 0); tee = -1; } /* EPIPE: reader gone */
        while (done < n) {
            long w = call(WRITE, 1, (long)(buf + done), n - done, 0, 0);
            if (w <= 0) call(EXIT, 0, 0, 0, 0, 0);
            done += w;
        }
    }
    call(EXIT, 0, 0, 0, 0, 0);
}
