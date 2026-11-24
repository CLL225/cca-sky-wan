import pandas as pd
import numpy as np
import argparse
import re
import os

def parse_ping_txt(ping_file):
    """解析 Ping TXT 日志（只提取成功的发包）"""
    data = []
    pattern = re.compile(r'\[(\d+\.\d+)\] .* time=([\d\.]+) ms')
    
    with open(ping_file, 'r', encoding='utf-8') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                data.append((float(match.group(1)), float(match.group(2))))
                
    df = pd.DataFrame(data, columns=['timestamp', 'rtt_ms'])
    return df.sort_values('timestamp')

def merge_lens_data(ping_txt, output_csv, base_bw=100.0, bw_jitter=10.0):
    """
    生成学术论文标准的高精度 LEO 卫星 Trace (100ms 粒度)
    """
    print("[*] 正在解析 Ping 原始数据...")
    df_ping = parse_ping_txt(ping_txt)

    if df_ping.empty:
        print("[!] 错误：Ping 数据解析为空。")
        return

    # ==========================================
    # 第一步：生成 100ms (0.1s) 级别的高精度时间轴
    # ==========================================
    start_time = df_ping['timestamp'].min()
    end_time = df_ping['timestamp'].max()
    
    print("[*] 正在生成 100ms 间隔的标准时间轴...")
    dense_timestamps = np.arange(np.floor(start_time), np.ceil(end_time) + 1, 0.1)
    df_final = pd.DataFrame({'timestamp': dense_timestamps})

    # ==========================================
    # 第二步：匹配 Ping 延迟数据
    # ==========================================
    df_final = pd.merge_asof(
        df_final, 
        df_ping[['timestamp', 'rtt_ms']], 
        on='timestamp', 
        direction='nearest', 
        tolerance=0.5 # 容忍度0.5秒：由于ping通常1秒1发，超0.5秒匹配不到视为丢包
    )

    # ==========================================
    # 第三步：判定丢包与处理 RTT（引入切换延迟尖峰和微抖动）
    # ==========================================
    np.random.seed(42) # 固定随机种子，保证每次生成的 Trace 完全一致，利于复现
    
    is_loss = df_final['rtt_ms'].isna()
    df_final['loss_rate'] = np.where(is_loss, 1.0, 0.0)
    
    # RTT 填充：使用线性插值 (interpolate) 让中间缺失的值平滑过渡，两端使用 40ms 兜底
    df_final['rtt_ms'] = df_final['rtt_ms'].interpolate(method='linear', limit_direction='both').fillna(40.0)

    # 模拟卫星切换前后的延迟尖峰 (Delay Spikes)
    # 找到丢包边缘：loss_rate 发生跳变的点 (从 0 变 1，或从 1 变 0)
    loss_edges = (df_final['loss_rate'].diff().abs() == 1.0)
    
    # 在断连边缘增加 50ms-150ms 的突发毛刺抖动，模拟底层链路重传导致的极高延迟
    spike_values = np.random.uniform(50.0, 150.0, size=loss_edges.sum())
    df_final.loc[loss_edges, 'rtt_ms'] += spike_values

    # 加入微小的高斯白噪声 (约 2ms) 模拟正常的链路微抖动
    df_final['rtt_ms'] += np.random.normal(loc=0.0, scale=2.0, size=len(df_final))
    # 保证 RTT 不低于典型的低轨物理极限（例如 20ms）
    df_final['rtt_ms'] = np.clip(df_final['rtt_ms'], 20.0, None)

    # ==========================================
    # 第四步：合成符合 LEO 特征的动态可用带宽（随机游走模型）
    # ==========================================
    # 使用随机游走 (Random Walk) 生成具备时间相关性、连续波动的带宽，而非生硬的白噪声
    steps = np.random.normal(loc=0, scale=bw_jitter * 0.1, size=len(df_final))
    synthetic_bw = base_bw + np.cumsum(steps)
    
    # 限制带宽在合理范围内 (基础带宽的 0.5倍 到 1.5倍 之间)，防止随机游走发散
    synthetic_bw = np.clip(synthetic_bw, base_bw * 0.5, base_bw * 1.5)
    df_final['bandwidth_mbps'] = synthetic_bw

    # 核心逻辑：断连期间，物理层无法传输，带宽必须降至极低
    # 设置为 0.01 而不是绝对的 0，是为了防止 tc qdisc 队列完全堵死导致内核或应用抛出意外错误
    df_final['bandwidth_mbps'] = np.where(df_final['loss_rate'] == 1.0, 0.01, df_final['bandwidth_mbps'])

    # 保留小数点，清理数据格式
    df_final['bandwidth_mbps'] = df_final['bandwidth_mbps'].round(2)
    df_final['rtt_ms'] = df_final['rtt_ms'].round(2)
    df_final['timestamp'] = df_final['timestamp'].round(1)

    # 输出文件
    df_final.to_csv(output_csv, index=False)
    print(f"\n[+] 处理完成！Paper-Ready 的高精度 LEO Trace 已保存至: {output_csv}")
    print(f"    -> 最终数据条数: {len(df_final)} 条 (100ms 精度)")
    print(f"    -> Trace 涵盖总时长: {len(df_final) / 10.0 / 60:.1f} 分钟")
    print(f"    -> 捕获到的卫星切换/断联次数: {loss_edges.sum() // 2} 次")
    print(f"    -> 正常连接时的平均带宽设定: ~{base_bw} Mbps")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成高精度 LEO 卫星网络 TC Trace")
    parser.add_argument("-p", "--ping", default="./ping.txt", help="Ping TXT 文件路径")
    parser.add_argument("-o", "--output", default="./trace.csv", help="输出的 CSV 文件路径")
    parser.add_argument("--base_bw", type=float, default=100.0, help="基准下行带宽 (Mbps)")
    parser.add_argument("--jitter", type=float, default=10.0, help="带宽波动范围参数 (Mbps)")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.ping):
        print(f"[!] 找不到文件: {args.ping}")
    else:
        merge_lens_data(args.ping, args.output, args.base_bw, args.jitter)