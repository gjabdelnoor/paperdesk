#include <linux/input.h>
#include <linux/uinput.h>

static long call(long n, long a, long b, long c, long d, long e, long f) {
#ifdef __aarch64__
    register long r8 __asm__("x8")=n, r0 __asm__("x0")=a;
    register long r1 __asm__("x1")=b, r2 __asm__("x2")=c;
    register long r3 __asm__("x3")=d, r4 __asm__("x4")=e, r5 __asm__("x5")=f;
    __asm__ volatile("svc #0" : "+r"(r0) : "r"(r8), "r"(r1), "r"(r2), "r"(r3), "r"(r4), "r"(r5) : "memory");
    return r0;
#else
    register long r10 __asm__("r10")=d, r8 __asm__("r8")=e, r9 __asm__("r9")=f;
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(n), "D"(a), "S"(b), "d"(c), "r"(r10), "r"(r8), "r"(r9) : "rcx", "r11", "memory");
    return result;
#endif
}
#ifdef __aarch64__
#define OPEN 56
#define READ 63
#define WRITE 64
#define CLOSE 57
#define IOCTL 29
#define POLL 73
#define CLOCK 113
#define SLEEP 101
#define EXIT 94
#else
#define OPEN 257
#define READ 0
#define WRITE 1
#define CLOSE 3
#define IOCTL 16
#define POLL 271
#define CLOCK 228
#define SLEEP 35
#define EXIT 231
#endif
static long sc(long n,long a,long b,long c) { return call(n,a,b,c,0,0,0); }
static int device=-1, created=0, failed=0, dry=0;
static struct input_absinfo axes[6];
static const int codes[6]={ABS_X,ABS_Y,ABS_PRESSURE,ABS_DISTANCE,ABS_TILT_X,ABS_TILT_Y};
static unsigned char pen_keys[(KEY_MAX+8)/8];
static long start;
static long now(void) {
    long ts[2];
    if(sc(CLOCK,1,(long)ts,0)<0) return -1;
    return ts[0]*1000+ts[1]/1000000;
}
static void zero(void *p,unsigned long n) { for(unsigned long i=0;i<n;i++) ((char*)p)[i]=0; }
static long ctl(int fd,unsigned long req,long arg) { return sc(IOCTL,fd,req,arg); }
static int output(const void *p,long n) { return sc(WRITE,1,(long)p,n)==n; }
static void emit(int type,int code,int value) {
    if(dry || device<0) return;
    struct input_event ev;
    zero(&ev,sizeof(ev)); ev.type=type; ev.code=code; ev.value=value;
    if(sc(WRITE,device,(long)&ev,sizeof(ev))!=sizeof(ev)) failed=1;
}
static void up(void) {
    emit(EV_ABS,ABS_PRESSURE,axes[2].minimum);
    emit(EV_ABS,ABS_DISTANCE,axes[3].maximum);
    emit(EV_KEY,BTN_TOUCH,0); emit(EV_KEY,BTN_TOOL_PEN,0);
    emit(EV_SYN,SYN_REPORT,0);
}
static void finish(int rc) {
    if(created) { up(); ctl(device,UI_DEV_DESTROY,0); }
    if(device>=0) sc(CLOSE,device,0,0);
    sc(EXIT,rc,0,0); for(;;) {}
}
static int read_exact(void *p,int size) {
    long deadline=now()+2000;
    int got=0;
    while(got<size) {
        long t=now(), remaining=deadline-t;
        if(t<0 || remaining<=0 || t-start>=30000) return 0;
        if(remaining>30000-(t-start)) remaining=30000-(t-start);
        struct { int fd; short events,revents; } pollfd={0,1,0};
        long timeout[2]={remaining/1000,(remaining%1000)*1000000};
        if(call(POLL,(long)&pollfd,1,(long)timeout,0,0,0)<=0) return 0;
        long n=sc(READ,0,(long)((char*)p+got),size-got);
        if(n<=0) return 0;
        got+=n;
    }
    return 1;
}
static unsigned int u32(const unsigned char *p) {
    return (unsigned int)p[0]|((unsigned int)p[1]<<8)|((unsigned int)p[2]<<16)|((unsigned int)p[3]<<24);
}
static struct input_id pen_id;
static unsigned long pen_props;
static void probe(void) {
    int fd=sc(OPEN,-100,(long)"/dev/input/event2",0);
    zero(pen_keys,sizeof(pen_keys));
    if(fd<0 || ctl(fd,EVIOCGID,(long)&pen_id)<0 ||
       ctl(fd,EVIOCGPROP(sizeof(pen_props)),(long)&pen_props)<0 ||
       ctl(fd,EVIOCGBIT(EV_KEY,sizeof(pen_keys)),(long)pen_keys)<0 ||
       !(pen_keys[BTN_TOOL_PEN/8]&(1<<(BTN_TOOL_PEN%8)))) finish(10);
    for(int i=0;i<6;i++) {
        if(ctl(fd,EVIOCGABS(codes[i]),(long)&axes[i])<0 ||
           axes[i].maximum<=axes[i].minimum || axes[i].minimum< -10000000 ||
           axes[i].maximum>10000000) finish(10);
    }
    if(axes[0].minimum<0 || axes[1].minimum<0 || axes[2].minimum!=0) finish(10);
    sc(CLOSE,fd,0,0);
}
static void setup(void) {
    device=sc(OPEN,-100,(long)"/dev/uinput",1|04000);
    if(device<0) finish(11);
    if(ctl(device,UI_SET_EVBIT,EV_KEY)<0 || ctl(device,UI_SET_EVBIT,EV_ABS)<0 ||
       ctl(device,UI_SET_EVBIT,EV_SYN)<0) finish(11);
    for(int bit=0;bit<=KEY_MAX;bit++)
        if((pen_keys[bit/8]&(1<<(bit%8))) && ctl(device,UI_SET_KEYBIT,bit)<0) finish(11);
    for(int bit=0;bit<32;bit++)
        if((pen_props&(1UL<<bit)) && ctl(device,UI_SET_PROPBIT,bit)<0) finish(11);
    for(int i=0;i<6;i++) {
        struct uinput_abs_setup a;
        zero(&a,sizeof(a)); a.code=codes[i]; a.absinfo=axes[i];
        if(i<3) a.absinfo.value=axes[i].minimum;
        else if(i==3) a.absinfo.value=axes[i].maximum;
        else a.absinfo.value=0;
        if(ctl(device,UI_SET_ABSBIT,codes[i])<0 || ctl(device,UI_ABS_SETUP,(long)&a)<0) finish(11);
    }
    struct uinput_setup u;
    zero(&u,sizeof(u)); u.id.bustype=BUS_VIRTUAL;
    const char *name="Experimental figure ink";
    for(int i=0;name[i];i++) u.name[i]=name[i];
    if(ctl(device,UI_DEV_SETUP,(long)&u)<0 || ctl(device,UI_DEV_CREATE,0)<0) finish(11);
    created=1;
    long delay[2]={0,500000000}; sc(SLEEP,(long)delay,0,0);
}
void _start(void) {
    unsigned char mode=0;
#ifdef __aarch64__
    const int sigmask_call=135, fcntl_call=25;
#else
    const int sigmask_call=14, fcntl_call=72;
#endif
    unsigned long mask=(1UL<<0)|(1UL<<1)|(1UL<<12)|(1UL<<14);
    if(call(sigmask_call,0,(long)&mask,0,8,0,0)<0 || sc(fcntl_call,1,4,04000)<0) finish(12);
    start=now();
    if(start<0 || !read_exact(&mode,1)) finish(12);
    if(mode=='V') dry=1;
    else if(mode=='I' || mode=='D') probe();
    else finish(12);
    if(mode=='I') { if(!output(axes,sizeof(axes))) finish(13); finish(0); }
    if(!dry) setup();
    if(!output("R",1)) finish(13);
    int down=0, strokes=0, records=0;
    for(;;) {
        unsigned char buf[9];
        if(!read_exact(buf,9)) finish(12);
        unsigned int x=u32(buf+1),y=u32(buf+5);
        int command=buf[0];
        if(command==3) {
            if(down || x || y) finish(12);
            up(); if(failed || !output("E",1)) finish(13); finish(0);
        }
        if(x>10000 || y>10000 || ++records>1200 ||
           (command==0 && (down || ++strokes>350)) ||
           (command==1 && !down) || (command==2 && !down) || command>2) finish(12);
        if(command==2) { up(); down=0; }
        else {
            for(int i=0;i<2;i++) {
                unsigned int v=i?y:x;
                int value=axes[i].minimum+(long)v*(axes[i].maximum-axes[i].minimum)/10000;
                emit(EV_ABS,codes[i],value);
            }
            emit(EV_KEY,BTN_TOOL_PEN,1);
            emit(EV_ABS,ABS_DISTANCE,axes[3].minimum);
            emit(EV_ABS,ABS_TILT_X,0); emit(EV_ABS,ABS_TILT_Y,0);
            emit(EV_ABS,ABS_PRESSURE,axes[2].maximum/4);
            emit(EV_KEY,BTN_TOUCH,1); emit(EV_SYN,SYN_REPORT,0); down=1;
        }
        if(failed) finish(13);
        long delay[2]={0,20000000}; sc(SLEEP,(long)delay,0,0);
        if(!output("A",1)) finish(13);
    }
}
