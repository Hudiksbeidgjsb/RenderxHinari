import requests
import socket
import threading

# 1. Scrape approx 100+ Free SOCKS5 Proxies
def get_proxies():
    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5",
        "https://www.proxy-list.download/api/v1/get?type=socks5"
    ]
    proxies = []
    for url in sources:
        try:
            r = requests.get(url, timeout=5)
            proxies.extend(r.text.strip().split('\r\n'))
        except: pass
    return list(set(proxies))

# 2. Check if Proxy is valid
def check_proxy(proxy, valid_list):
    try:
        ip, port = proxy.split(':')
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        if s.connect_ex((ip, int(port))) == 0:
            valid_list.append(proxy)
            print(f"[+] Valid Proxy: {proxy}")
        s.close()
    except: pass

def main():
    print("[*] Scraping Proxies...")
    all_proxies = get_proxies()
    valid_proxies = []
    threads = []
    
    print(f"[*] Checking {len(all_proxies)} proxies...")
    for p in all_proxies[:300]: # Check up to 300 to find 100 good ones
        t = threading.Thread(target=check_proxy, args=(p, valid_proxies))
        t.start()
        threads.append(t)
    
    for t in threads: t.join()
    
    with open("valid_proxies.txt", "w") as f:
        f.write("\n".join(valid_proxies[:100]))
    print(f"[!] Saved {len(valid_proxies[:100])} valid proxies to valid_proxies.txt")

if __name__ == "__main__":
    main()
  
