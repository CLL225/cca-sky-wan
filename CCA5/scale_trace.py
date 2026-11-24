import pandas as pd
import numpy as np
import argparse

def scale_trace_time(input_csv, output_csv, compress_ratio=30):
    print(f"[*] 正在读取原始 Trace: {input_csv}")
    df = pd.read_csv(input_csv)

    if df.empty:
        print("[!] 错误：输入文件为空")
        return

    # ==========================================
    # 核心逻辑：时间压缩（下采样 Speed-up）
    # ==========================================
    # 默认 compress_ratio=30。
    # 意味着每隔 30 行（30秒）抽取 1 行作为新环境的 1 秒。
    # 原本 60 秒的带宽平台期，缩放后就变成了 60 / 30 = 2 秒！
    df_scaled = df.iloc[::compress_ratio].copy().reset_index(drop=True)

    # ==========================================
    # 重建时间轴
    # ==========================================
    # 为了保证 TC 工具不报错，时间必须是严格连续递增的 1 秒间隔。
    # 我们保留原始的起始时间戳，并往后以 1.0 秒间隔重新生成。
    start_time = df['timestamp'].iloc[0]
    df_scaled['timestamp'] = start_time + np.arange(len(df_scaled), dtype=float)

    # 格式化输出文件
    df_scaled.to_csv(output_csv, index=False)

    print(f"\n[+] 处理完成！缩放后的 Trace 已保存至: {output_csv}")
    print(f"    -> 原始 Trace 总时长: {len(df)} 秒 ({len(df)/60:.1f} 分钟)")
    print(f"    -> 缩放后 Trace 总时长: {len(df_scaled)} 秒 ({len(df_scaled)/60:.1f} 分钟)")
    print(f"    -> 缩放后带宽变化频率: 每 {60 // compress_ratio} 秒更新一次")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="卫星网络 Trace 时间压缩器")
    parser.add_argument("-i", "--input", default="./bin/traces/satellite/trace.csv", help="你之前生成的 trace.csv")
    parser.add_argument("-o", "--output", default="./bin/traces/satellite_scaled/trace_scaled.csv", help="输出的缩放版文件")
    parser.add_argument("-c", "--compress", type=int, default=60, help="压缩倍率(默认30，即将60s压缩为2s)")

    args = parser.parse_args()
    scale_trace_time(args.input, args.output, args.compress)