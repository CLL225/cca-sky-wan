import subprocess

INTERFACE = "veth_client"
NAMESPACE = "client_ns"

def setup_wan(profile):
    """
    配置静态 WAN 网络
    使用 Netem 控制延迟和丢包，使用 TBF (Token Bucket Filter) 控制带宽和队列长度
    """
    run_cmd(f"tc qdisc del dev {INTERFACE} root")
    
    if profile == "inter_city":
        # 城域网/跨省: 100Mbps, 40ms RTT (单向20ms), 0%丢包, 队列 1MB
        run_cmd(f"tc qdisc add dev {INTERFACE} root netem delay 20ms rate 100mbit limit 1000")
        
    elif profile == "trans_oceanic":
        # 跨洋链路 (大BDP): 500Mbps, 150ms RTT (单向75ms), 0.1%随机丢包, 队列深度很大
        # RTT 150ms 存在微小抖动 (jitter 5ms)
        run_cmd(f"tc qdisc add dev {INTERFACE} root netem delay 75ms 5ms loss 0.1% rate 500mbit limit 5000")
        
    elif profile == "lossy_wan":
        # 劣质广域网 (抗丢包测试): 50Mbps, 100ms RTT (单向50ms), 2%随机丢包
        run_cmd(f"tc qdisc add dev {INTERFACE} root netem delay 50ms loss 2.0% rate 50mbit limit 500")

def run_cmd(cmd):
    full_cmd = f"ip netns exec {NAMESPACE} {cmd}"
    subprocess.run(full_cmd, shell=True)
    print(f"[+] Executed: {cmd}")

# 调用示例
setup_wan("trans_oceanic")