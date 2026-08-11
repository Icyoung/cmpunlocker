/* prove_80g_b.c — supplementary: single 34G OK, dual-fork OK */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <cuda_runtime.h>
#define GB (1024ULL*1024*1024)
#define CHK(x) do{cudaError_t e=(x);if(e){printf("FAIL %s\n",cudaGetErrorString(e));return 1;}}while(0)

static int sample(void *p, unsigned char exp) {
    unsigned char b[1048576];
    CHK(cudaMemcpy(b, p, sizeof(b), cudaMemcpyDeviceToHost));
    size_t bad=0; for(size_t i=0;i<sizeof(b);i++) if(b[i]!=exp) bad++;
    printf("  sample 1MiB bad=%zu %s\n", bad, bad?"FAIL":"OK");
    return bad?1:0;
}

static int child(size_t g, int pat) {
    void *p=NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&p, g*GB));
    CHK(cudaMemset(p, pat, g*GB));
    CHK(cudaDeviceSynchronize());
    unsigned char b=0; CHK(cudaMemcpy(&b,p,1,cudaMemcpyDeviceToHost));
    printf("  fork child %zuG pat=0x%02X byte=0x%02X pid=%d\n", g, pat, b, getpid());
    fflush(stdout);
    for(;;) pause();
    return 0;
}

int main(void) {
    size_t free=0,total=0;
    CHK(cudaSetDevice(0));
    CHK(cudaMemGetInfo(&free,&total));
    printf("cuda total=%.2f GiB free=%.2f GiB\n", total/(double)GB, free/(double)GB);

    printf("\n[1] single 34G fill 0xAA\n");
    void *a=NULL; CHK(cudaMalloc(&a,34*GB));
    CHK(cudaMemset(a,0xAA,34*GB)); CHK(cudaDeviceSynchronize());
    int r1=sample(a,0xAA); CHK(cudaFree(a));

    printf("\n[2] another single 34G fill 0xBB (after free first)\n");
    void *b=NULL; CHK(cudaMalloc(&b,34*GB));
    CHK(cudaMemset(b,0xBB,34*GB)); CHK(cudaDeviceSynchronize());
    int r2=sample(b,0xBB); CHK(cudaFree(b));

    printf("\n[3] dual fork 34G+34G AA/BB\n");
    pid_t p1=fork(); if(p1==0) return child(34,0xAA);
    pid_t p2=fork(); if(p2==0) return child(34,0xBB);
    sleep(8);
    system("nvidia-smi --query-gpu=memory.used --format=csv,noheader");
    kill(p1,SIGTERM); kill(p2,SIGTERM);
    waitpid(p1,NULL,0); waitpid(p2,NULL,0);

    printf("\nSUMMARY: single34=%s seq34=%s (dual mem see smi)\n",
           r1?"FAIL":"OK", r2?"FAIL":"OK");
    return r1|r2;
}
