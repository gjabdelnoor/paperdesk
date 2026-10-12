/*
 * remarkable-type touch injector — runs on the reMarkable Paper Pro.
 *
 * Creates one direct-touch uinput device.  stdin is a stream of five-byte
 * records:
 *   [command][x little-endian uint16][y little-endian uint16]
 *   command 0 = finger down, 1 = move, 2 = finger up
 *
 * The ranges mirror the Paper Pro's Elan touch panel.  The binary is built
 * freestanding/static so it does not depend on userspace libraries on the
 * tablet.
 */

#include <linux/uinput.h>
#include <linux/input.h>

static long syscall6(long n, long a, long b, long c, long d, long e, long f) {
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile("svc #0"
                     : "+r"(x0)
                     : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
                     : "memory");
    return x0;
}
static long syscall3(long n, long a, long b, long c) {
    return syscall6(n, a, b, c, 0, 0, 0);
}
static long syscall1(long n, long a) {
    return syscall6(n, a, 0, 0, 0, 0, 0);
}

#define SYS_openat     56
#define SYS_ioctl      29
#define SYS_read       63
#define SYS_write      64
#define SYS_close      57
#define SYS_exit_group 94

#define AT_FDCWD (-100)
#define O_WRONLY 00000001

#define TOUCH_MAX_X 2064
#define TOUCH_MAX_Y 2832

static long openat_(const char *path, int flags) {
    return syscall3(SYS_openat, AT_FDCWD, (long)path, flags);
}
static long ioctl_(int fd, unsigned long req, void *arg) {
    return syscall3(SYS_ioctl, fd, req, (long)arg);
}
static long read_(int fd, void *buf, unsigned long n) {
    return syscall3(SYS_read, fd, (long)buf, n);
}
static long write_(int fd, const void *buf, unsigned long n) {
    return syscall3(SYS_write, fd, (long)buf, n);
}
static long close_(int fd) { return syscall1(SYS_close, fd); }
static void exit_(int code) {
    syscall1(SYS_exit_group, code);
    for (;;) {}
}

static void zero_(void *ptr, unsigned long n) {
    unsigned long i;
    for (i = 0; i < n; i++) ((unsigned char *)ptr)[i] = 0;
}

static void emit(int fd, unsigned short type, unsigned short code, int value) {
    struct input_event ev;
    unsigned long off = 0;
    zero_(&ev, sizeof(ev));
    ev.type = type;
    ev.code = code;
    ev.value = value;
    while (off < sizeof(ev)) {
        long n = write_(fd, (const char *)&ev + off, sizeof(ev) - off);
        if (n <= 0) exit_(3);
        off += (unsigned long)n;
    }
}

static void setup_abs(int fd, unsigned short code, int maximum, int resolution) {
    struct uinput_abs_setup setup;
    zero_(&setup, sizeof(setup));
    setup.code = code;
    setup.absinfo.minimum = 0;
    setup.absinfo.maximum = maximum;
    setup.absinfo.resolution = resolution;
    if (ioctl_(fd, UI_ABS_SETUP, &setup) < 0) exit_(2);
}

static int clamp(int value, int maximum) {
    if (value < 0) return 0;
    if (value > maximum) return maximum;
    return value;
}

static void position(int fd, int x, int y) {
    emit(fd, EV_ABS, ABS_MT_SLOT, 0);
    emit(fd, EV_ABS, ABS_MT_POSITION_X, clamp(x, TOUCH_MAX_X));
    emit(fd, EV_ABS, ABS_MT_POSITION_Y, clamp(y, TOUCH_MAX_Y));
}

void _start(void) {
    int fd = openat_("/dev/uinput", O_WRONLY);
    int down = 0;
    int tracking_id = 1;
    if (fd < 0) exit_(1);

    if (ioctl_(fd, UI_SET_EVBIT, (void *)(long)EV_SYN) < 0) exit_(2);
    if (ioctl_(fd, UI_SET_EVBIT, (void *)(long)EV_KEY) < 0) exit_(2);
    if (ioctl_(fd, UI_SET_EVBIT, (void *)(long)EV_ABS) < 0) exit_(2);
    if (ioctl_(fd, UI_SET_KEYBIT, (void *)(long)BTN_TOUCH) < 0) exit_(2);
    if (ioctl_(fd, UI_SET_PROPBIT, (void *)(long)INPUT_PROP_DIRECT) < 0) exit_(2);

    setup_abs(fd, ABS_MT_SLOT, 9, 0);
    setup_abs(fd, ABS_MT_TOUCH_MAJOR, 255, 0);
    setup_abs(fd, ABS_MT_POSITION_X, TOUCH_MAX_X, TOUCH_MAX_X);
    setup_abs(fd, ABS_MT_POSITION_Y, TOUCH_MAX_Y, TOUCH_MAX_Y);
    setup_abs(fd, ABS_MT_TOOL_TYPE, 2, 0);
    setup_abs(fd, ABS_MT_TRACKING_ID, 65535, 0);
    setup_abs(fd, ABS_MT_PRESSURE, 255, 0);
    setup_abs(fd, ABS_MT_DISTANCE, 255, 0);

    {
        struct uinput_setup setup;
        const char *name = "reMarkable Remote Touch";
        unsigned long i;
        zero_(&setup, sizeof(setup));
        setup.id.bustype = BUS_SPI;
        setup.id.version = 1;
        for (i = 0; i < sizeof(setup.name) - 1 && name[i]; i++)
            setup.name[i] = name[i];
        if (ioctl_(fd, UI_DEV_SETUP, &setup) < 0) exit_(2);
        if (ioctl_(fd, UI_DEV_CREATE, 0) < 0) exit_(2);
    }

    for (;;) {
        unsigned char buf[5];
        long got = 0;
        int x, y;
        while (got < 5) {
            long n = read_(0, buf + got, 5 - got);
            if (n <= 0) break;
            got += n;
        }
        if (got < 5) break;
        x = (int)buf[1] | ((int)buf[2] << 8);
        y = (int)buf[3] | ((int)buf[4] << 8);

        if (buf[0] == 0) {
            if (down) {
                emit(fd, EV_KEY, BTN_TOUCH, 0);
                emit(fd, EV_ABS, ABS_MT_TRACKING_ID, -1);
                emit(fd, EV_SYN, SYN_REPORT, 0);
            }
            position(fd, x, y);
            emit(fd, EV_ABS, ABS_MT_TRACKING_ID, tracking_id++);
            emit(fd, EV_ABS, ABS_MT_TOUCH_MAJOR, 10);
            emit(fd, EV_ABS, ABS_MT_PRESSURE, 128);
            emit(fd, EV_ABS, ABS_MT_DISTANCE, 0);
            emit(fd, EV_KEY, BTN_TOUCH, 1);
            emit(fd, EV_SYN, SYN_REPORT, 0);
            down = 1;
        } else if (buf[0] == 1 && down) {
            position(fd, x, y);
            emit(fd, EV_SYN, SYN_REPORT, 0);
        } else if (buf[0] == 2 && down) {
            position(fd, x, y);
            emit(fd, EV_KEY, BTN_TOUCH, 0);
            emit(fd, EV_ABS, ABS_MT_PRESSURE, 0);
            emit(fd, EV_ABS, ABS_MT_TOUCH_MAJOR, 0);
            emit(fd, EV_ABS, ABS_MT_TRACKING_ID, -1);
            emit(fd, EV_SYN, SYN_REPORT, 0);
            down = 0;
        }
    }

    if (down) {
        emit(fd, EV_KEY, BTN_TOUCH, 0);
        emit(fd, EV_ABS, ABS_MT_TRACKING_ID, -1);
        emit(fd, EV_SYN, SYN_REPORT, 0);
    }
    ioctl_(fd, UI_DEV_DESTROY, 0);
    close_(fd);
    exit_(0);
}
