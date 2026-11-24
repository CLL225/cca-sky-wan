import time
import subprocess
import csv
import argparse
import os

# ==========================================
# 固定网络配置参数
# ==========================================
INTERFACE = "veth_client"
NAMESPACE = "client_ns"

def run_cmd(cmd, ignore_errors=False):
    """在指定的命名空间内执行命令"""
    full_cmd = f"ip netns exec {NAMESPACE} {cmd}"
    try:
        subprocess.run(full_cmd, shell=True, check=not ignore_errors, 
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        if not ignore_errors:
            print(f"[!] 命令执行失败: {full_cmd}")

def setup_tc():
    """初始化 TC 规则：构建 HTB (控带宽) + Netem (控延迟与丢包) 的级联结构"""
    print(f"[*] 正在初始化命名空间 {NAMESPACE} 中 {INTERFACE} 的 tc 规则...")
    # 1. 清除旧规则（如果存在忽略报错）
    run_cmd(f"tc qdisc del dev {INTERFACE} root", ignore_errors=True)
    
    # 2. 建立 HTB 根节点
    run_cmd(f"tc qdisc add dev {INTERFACE} root handle 1: htb default 10")
    
    # 3. 建立 HTB Class 用于控制带宽 (初始设置为 1000Mbps)
    run_cmd(f"tc class add dev {INTERFACE} parent 1: classid 1:10 htb rate 1000mbit")
    
    # 4. 在 HTB Class 下挂载 Netem 队列用于控制延迟和丢包 (初始 0ms)
    run_cmd(f"tc qdisc add dev {INTERFACE} parent 1:10 handle 10: netem delay 0ms")

def teardown_tc():
    """清理 TC 规则"""
    print(f"\n[*] 正在清理 {INTERFACE} 的 tc 规则...")
    run_cmd(f"tc qdisc del dev {INTERFACE} root", ignore_errors=True)
    print("[+] 清理完成。")

def update_tc(bandwidth_mbps, delay_ms, loss_percent):
    """动态更新带宽、延迟和丢包率"""
    # 更新 HTB 带宽
    cmd_bw = f"tc class change dev {INTERFACE} parent 1: classid 1:10 htb rate {bandwidth_mbps}mbit"
    # 更新 Netem 延迟与丢包
    cmd_netem = f"tc qdisc change dev {INTERFACE} parent 1:10 handle 10: netem delay {delay_ms}ms loss {loss_percent}%"
    
    run_cmd(cmd_bw)
    run_cmd(cmd_netem)

def replay_trace(trace_file):
    """核心高精度重放逻辑"""
    if not os.path.exists(trace_file):
        print(f"[!] 错误: 找不到 Trace 文件 '{trace_file}'")
        exit(1)

    setup_tc()
    print(f"[*] 开始高精度重放网络 Trace: {trace_file} (按 Ctrl+C 终止)")
    
    try:
        with open(trace_file, 'r') as f:
            reader = list(csv.DictReader(f)) # 将其转为 list 以便获取 index
            total_lines = len(reader)
            
            # 记录起始高精度时间
            start_time = time.perf_counter()
            
            for index, row in enumerate(reader):
                try:
                    # 目标时间点 (100ms = 0.1s 粒度)
                    target_time = start_time + (index * 0.1)
                    
                    bw = float(row['bandwidth_mbps'])
                    delay = float(row['rtt_ms']) / 2.0  # 单向延迟
                    loss = float(row['loss_rate']) * 100.0
                    
                    # 遇到断连时，带宽设为极小值，丢包 100%
                    if bw <= 0.01 or loss >= 100.0:
                        update_tc(0.01, delay, 100.0) 
                        current_bw, current_loss = 0.01, 100.0
                    else:
                        loss=0.07
                        update_tc(bw, delay, loss)
                        current_bw, current_loss = bw, loss
                    
                    # === 核心：时间补偿机制 ===
                    now = time.perf_counter()
                    # 计算距离下一个 0.1s 周期还剩多少时间
                    sleep_time = target_time + 0.1 - now
                    
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                        
                    # 减少打印频率，防止 IO 拖慢循环速度（每 20 行 / 2 秒打印一次）
                    if index % 20 == 0 and index > 0:
                        print(f"  -> 进度: {index}/{total_lines} ({index*0.1:.1f}s) | BW={current_bw:.2f}Mbps, Delay={delay:.2f}ms, Loss={current_loss:.1f}%")
                        
                except ValueError as e:
                    print(f"[!] 数据解析错误，跳过该行: {row} - {e}")
                except KeyError as e:
                    print(f"[!] CSV 缺少必要的列名: {e}，请检查 CSV 表头")
                    exit(1)
                    
    except KeyboardInterrupt:
        print("\n[!] 用户手动中断重放。")
    finally:
        teardown_tc()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LENS 卫星网络 Trace 动态高精度重放工具 (TC/NetEm)")
    parser.add_argument(
        "-t", "--trace", 
        required=True, 
        help="LENS CSV Trace 文件的绝对或相对路径"
    )
    args = parser.parse_args()
    
    # 执行重放
    replay_trace(args.trace)