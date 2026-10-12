/* SPDX-License-Identifier: AGPL-3.0-or-later
 * PaperDesk uinput keyboard. Freestanding ARM32/AArch64, no tablet libc.
 * stdin records: [0,keycode] down, [1,keycode] up, [2,0] sync.
 * One process per authenticated typing session; EOF destroys its device.
 */
typedef unsigned short u16;
typedef unsigned int u32;
#if defined(__aarch64__)
static long call(long n,long a,long b,long c) {
    register long x8 __asm__("x8")=n, x0 __asm__("x0")=a;
    register long x1 __asm__("x1")=b, x2 __asm__("x2")=c;
    __asm__ volatile("svc 0" : "+r"(x0):"r"(x8),"r"(x1),"r"(x2):"memory");
    return x0;
}
#define READ 63
#define WRITE 64
#define IOCTL 29
#define CLOSE 57
#define EXIT 94
static long open_device(void) {return call(56,-100,(long)"/dev/uinput",1);}
#elif defined(__arm__)
static long call(long n,long a,long b,long c) {
    register long r7 __asm__("r7")=n, r0 __asm__("r0")=a;
    register long r1 __asm__("r1")=b, r2 __asm__("r2")=c;
    __asm__ volatile("svc 0" : "+r"(r0):"r"(r7),"r"(r1),"r"(r2):"memory");
    return r0;
}
#define READ 3
#define WRITE 4
#define IOCTL 54
#define CLOSE 6
#define EXIT 248
static long open_device(void) {return call(5,(long)"/dev/uinput",1,0);}
#else
#error Unsupported tablet architecture
#endif
static void quit(int status) {call(EXIT,status,0,0);for(;;){}}
static void zero(void *ptr, unsigned long n) {unsigned char *p=ptr;while(n--)*p++=0;}
struct event {long seconds,microseconds;u16 type,code;int value;};
struct setup {u16 bus,vendor,product,version;char name[80];u32 effects;};
static void emit(int fd,u16 type,u16 code,int value) {
    struct event e;zero(&e,sizeof e);e.type=type;e.code=code;e.value=value;
    unsigned long done=0;
    while(done<sizeof e){long n=call(WRITE,fd,(long)((char*)&e+done),sizeof e-done);if(n<=0)quit(3);done+=n;}
}
void _start(void) {
    int fd=open_device();if(fd<0)quit(1);
    if(call(IOCTL,fd,0x40045564,0)<0||call(IOCTL,fd,0x40045564,1)<0||call(IOCTL,fd,0x40045564,20)<0)quit(2);
    for(int key=0;key<=767;key++)if(call(IOCTL,fd,0x40045565,key)<0)quit(2);
    struct setup s;zero(&s,sizeof s);s.bus=3;s.vendor=0x04b3;s.product=0x4010;s.version=1;
    /* Retain the device identity used by the original, working Type injector. */
    const char *name="reMarkable Type Folio";for(unsigned long i=0;name[i]&&i<79;i++)s.name[i]=name[i];
    if(call(IOCTL,fd,0x405c5503,(long)&s)<0||call(IOCTL,fd,0x5501,0)<0)quit(2);
    for(;;){unsigned char record[2];int done=0;while(done<2){long n=call(READ,0,(long)(record+done),2-done);if(n<=0)break;done+=n;}if(done!=2)break;
      if(record[0]<2)emit(fd,1,record[1],record[0]==0?1:0);
      if(record[0]<=2)emit(fd,0,0,0);
    }
    call(IOCTL,fd,0x5502,0);call(CLOSE,fd,0,0);quit(0);
}
