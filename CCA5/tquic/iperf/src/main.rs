use clap::Parser;
use bytes::Bytes;
// use nix::sys::socket;
// use tquic::endpoint;
use core::panic;
use std::fs::{create_dir_all, File};
use std::io::{BufWriter, Write};
use std::net::SocketAddr;
use mio::event::Event;
use mio::net::UdpSocket;
use mio::Interest;
use mio::Token;
use slab::Slab;
use std::rc::Rc;
use std::cell::Cell;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tquic::{Config, Connection, Endpoint, PacketInfo, PacketSendHandler, TlsConfig, TransportHandler};
use nix::libc::{iovec, mmsghdr, msghdr, sockaddr_in, sockaddr_in6, sockaddr_storage};
// use log::debug;
use log::error;
use log::info;
use log::warn;
use std::mem;
use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::io::ErrorKind;
use rustc_hash::FxHashMap;
use rand::Rng;

// ==========================================
// 新增: 视频播放器状态及处理逻辑
// ==========================================
#[derive(Clone, Default)]
pub struct VideoPlayerState {
    pub recv_buffer: Vec<u8>,
    pub first_frame_time: Option<Instant>,
    pub expected_play_time: Option<Instant>,
    pub total_play_time: Duration,
    pub total_stall_time: Duration,
    pub frames_received: u32,
}

impl VideoPlayerState {
    pub fn process_frames(&mut self) {
        let fps = 30.0;
        let frame_interval = Duration::from_secs_f64(1.0 / fps);
        let initial_delay = Duration::from_millis(500); // 初始缓冲 500ms

        while self.recv_buffer.len() >= 4 {
            let mut len_bytes = [0u8; 4];
            len_bytes.copy_from_slice(&self.recv_buffer[0..4]);
            let frame_len = u32::from_be_bytes(len_bytes) as usize;

            if self.recv_buffer.len() >= 4 + frame_len {
                let now = Instant::now();
                if self.first_frame_time.is_none() {
                    self.first_frame_time = Some(now);
                    self.expected_play_time = Some(now + initial_delay);
                }

                let mut expected = self.expected_play_time.unwrap();
                if now > expected {
                    self.total_stall_time += now.duration_since(expected);
                    expected = now; // 卡顿导致期望播放时钟向后平移
                }

                expected += frame_interval;
                self.expected_play_time = Some(expected);
                self.total_play_time += frame_interval;
                self.frames_received += 1;

                // 移除已处理完的一帧（包括 4字节头部 和 frame_len 载荷）
                self.recv_buffer.drain(..(4 + frame_len));
            } else {
                // 不够拼装一帧，等待下一次读包
                break;
            }
        }
    }
}

#[derive(Parser, Debug)]
#[clap(name = "iperf", version=env!("CARGO_PKG_VERSION"))]
pub struct IperfOpt {
    #[clap(short, long, default_value = "0.0.0.0:4433", value_name = "ADDR")]
    pub listen: SocketAddr,
    #[clap(short, long, default_value = "false", value_name = "SERVER")]
    pub server: bool,
    #[clap(long, value_name = "CONNECT_ADDR")]
    pub connect_to: Option<SocketAddr>,
    #[clap(short, long, default_value = "5", value_name = "DURATION")]
    pub duration: u64,
    #[clap(short, long, default_value = "bbr", value_name = "CC")]
    pub congestion_control: String,
    #[clap(long, value_name = "GENERATED CONGESTION CONTROL FILENAME FOR LOG PRINT")]
    pub cca_filename: Option<String>,
    #[clap(long, value_name = "TRACE FILENAME FOR LOG PRINT")]
    pub trace: Option<String>,
    #[clap(long, default_value = "STREAM", value_name = "TOOL MODE IPERF, STREAM OR TOOLSTREAM")]
    pub mode: String,
    #[clap(long, default_value = "0", value_name = "SIZE OF DATA FOR TRANSMISSION")]
    pub data_size: u64,
    #[clap(long, value_name = "PATH")]
    pub timeseries_output: Option<String>,
    #[clap(long, default_value = "1000", value_name = "MS")]
    pub sample_interval_ms: u64,
}

struct Iperf {
    endpoint: Endpoint,
    sender: Rc<UdpSender>,
    recv_buf: [u8; 65535],
    poll: mio::Poll,
    conn_id: u64,
    bytes_transferred: Arc<Mutex<u64>>,
    stop: Rc<Cell<bool>>,
    stop_writing: Rc<Cell<bool>>,
}

impl Iperf {
    fn new(socket: UdpSocket, args: &IperfOpt) -> Self {
        let server = args.server;
        let mut config = Config::new().unwrap();
        config.set_max_idle_timeout(60000); 
        config.set_initial_max_data(10 * 1024 * 1024 * 1024);
        config.set_initial_max_stream_data_bidi_local(10 * 1024 * 1024 * 1024);
        config.set_initial_max_stream_data_bidi_remote(10 * 1024 * 1024 * 1024);

        let mut tls_config = TlsConfig::new().unwrap();
        let _ = tls_config.set_application_protos(vec![b"tquic-perf".to_vec()]);
        
        if server {
            tls_config.set_certificate_file("cert.crt").unwrap();
            tls_config.set_private_key_file("cert.key").unwrap();
        }
        config.set_tls_config(tls_config);

        let congestion_control = args.congestion_control.to_ascii_lowercase();
        match congestion_control.as_str() {
            "bbr" => {
                config.set_congestion_control_algorithm(tquic::CongestionControlAlgorithm::Bbr)
            }
            "cubic" => {
                config.set_congestion_control_algorithm(tquic::CongestionControlAlgorithm::Cubic)
            }
            "copa" => {
                config.set_congestion_control_algorithm(tquic::CongestionControlAlgorithm::Copa)
            }
            "llmgen" => {
                config.set_congestion_control_algorithm(tquic::CongestionControlAlgorithm::LLMGen)
            }
            "mutant" => {
                config.set_congestion_control_algorithm(tquic::CongestionControlAlgorithm::Mutant)
            }
            _ => {
                error!("不支持的拥塞控制算法: {}", args.congestion_control);
                panic!("不支持的拥塞控制算法");
            }
        }
        
        let poll = match mio::Poll::new() {
            Ok(p) => p,
            Err(e) => {
                error!("创建 Poll 失败: {:?}", e);
                panic!("无法创建 Poll");
            }
        };
        let bytes_transferred = Arc::new(Mutex::new(0u64));
        let stop = Rc::new(Cell::new(false));
        let stop_writing = Rc::new(Cell::new(false));

        let handler = Box::new(PerfHandler {
            server: server,
            bytes_transferred: bytes_transferred.clone(),
            mode: args.mode.clone().to_uppercase(),
            data_size: args.data_size,
            next_send_time: None,
            pending_bytes: 0,
            frame_interval: Duration::from_millis(33),
            send_buffer: Vec::new(),
            player_state: VideoPlayerState::default(),
            stop: stop.clone(),
            stop_writing: stop_writing.clone(),
        });
        let sender = Rc::new(UdpSender::new(socket, poll.registry()));
        
        Self {
            endpoint: Endpoint::new(Box::new(config), server, handler, sender.clone()),
            sender,
            recv_buf: [0; 65535],
            poll,
            conn_id: 0,
            bytes_transferred,
            stop,
            stop_writing,
        }
    }

    fn process_read_event(&mut self, event: &Event) -> Result<(), std::io::Error> {
        let socket = match self.sender.socks.get(event.token().0) {
            Some(socket) => socket,
            None => return Err(std::io::Error::new(ErrorKind::Other, "invalid token")),
        };
        loop {
            let (len, remote) = match socket.recv_from(&mut self.recv_buf) {
                Ok(v) => v,
                Err(e) => {
                    if e.kind() == std::io::ErrorKind::WouldBlock {
                        break;
                    }
                    error!("socket recv error: {:?}", e);
                    return Err(e);
                }
            };

            let pkt_buf = &mut self.recv_buf[..len];
            let pkt_info = PacketInfo {
                src: remote,
                dst: self.sender.local_addr,
                time: Instant::now(),
            };

            match self.endpoint.recv(pkt_buf, &pkt_info) {
                Ok(_) => {}
                Err(e) => {
                    error!("recv failed: {:?}", e);
                    continue;
                }
            };
        }
        Ok(())
    }
}

fn socket_addr_to_storage(addr: &std::net::SocketAddr) -> (sockaddr_storage, nix::libc::socklen_t) {
    let mut storage: sockaddr_storage = unsafe { mem::zeroed() };
    match addr {
        std::net::SocketAddr::V4(addr_v4) => {
            let sin = sockaddr_in {
                sin_family: nix::libc::AF_INET as u16,
                sin_port: addr_v4.port().to_be(),
                sin_addr: nix::libc::in_addr {
                    s_addr: u32::from_ne_bytes(addr_v4.ip().octets()),
                },
                sin_zero: [0; 8],
            };
            unsafe {
                std::ptr::copy_nonoverlapping(
                    &sin as *const _ as *const u8,
                    &mut storage as *mut _ as *mut u8,
                    mem::size_of::<sockaddr_in>(),
                );
            }
            (storage, mem::size_of::<sockaddr_in>() as nix::libc::socklen_t)
        }
        std::net::SocketAddr::V6(addr_v6) => {
            let sin6 = sockaddr_in6 {
                sin6_family: nix::libc::AF_INET6 as u16,
                sin6_port: addr_v6.port().to_be(),
                sin6_flowinfo: addr_v6.flowinfo(),
                sin6_addr: nix::libc::in6_addr {
                    s6_addr: addr_v6.ip().octets(),
                },
                sin6_scope_id: addr_v6.scope_id(),
            };
            unsafe {
                std::ptr::copy_nonoverlapping(
                    &sin6 as *const _ as *const u8,
                    &mut storage as *mut _ as *mut u8,
                    mem::size_of::<sockaddr_in6>(),
                );
            }
            (storage, mem::size_of::<sockaddr_in6>() as nix::libc::socklen_t)
        }
    }
}

struct UdpSender {
    socks: Slab<UdpSocket>,
    local_addr: SocketAddr,
    addrs: FxHashMap<SocketAddr, usize>,
}

impl UdpSender {
    fn new(socket: UdpSocket, registry: &mio::Registry) -> Self {
        let local_addr = socket.local_addr().expect("无法获取本地地址");
        let mut socks = Slab::new();
        let sid = socks.insert(socket);
        let sock = socks.get_mut(sid).unwrap();
        registry.register(sock, Token(sid), Interest::READABLE).expect("无法注册 socket");

        let mut addrs = FxHashMap::default();
        addrs.insert(local_addr, sid);

        Self { socks, local_addr, addrs }
    }
}

impl PacketSendHandler for UdpSender {
    fn on_packets_send(&self, pkts: &[(Vec<u8>, PacketInfo)]) -> tquic::Result<usize> {
        if pkts.is_empty() { return Ok(0); }
        
        let mut msgvec: Vec<mmsghdr> = Vec::with_capacity(pkts.len());
        let mut iovecs: Vec<iovec> = Vec::with_capacity(pkts.len());
        let mut addrs: Vec<sockaddr_storage> = Vec::with_capacity(pkts.len());
        let mut addr_lens: Vec<nix::libc::socklen_t> = Vec::with_capacity(pkts.len());

        for (pkt, info) in pkts {
            iovecs.push(iovec {
                iov_base: pkt.as_ptr() as *mut nix::libc::c_void,
                iov_len: pkt.len(),
            });
            let (addr, addr_len) = socket_addr_to_storage(&info.dst);
            addrs.push(addr);
            addr_lens.push(addr_len);
        }

        for i in 0..pkts.len() {
            let mut msg: msghdr = unsafe { mem::zeroed() };
            msg.msg_name = &mut addrs[i] as *mut _ as *mut nix::libc::c_void;
            msg.msg_namelen = addr_lens[i];
            msg.msg_iov = &mut iovecs[i] as *mut iovec;
            msg.msg_iovlen = 1;
            let mmsg = mmsghdr { msg_hdr: msg, msg_len: 0 };
            msgvec.push(mmsg);
        }

        let sid = self.addrs.get(&self.local_addr).ok_or(tquic::Error::InvalidOperation("未找到本地 socket".into()))?;
        let local_sock = self.socks.get(*sid).ok_or(tquic::Error::InvalidOperation("未找到本地 socket".into()))?;

        let ret = unsafe { nix::libc::sendmmsg(local_sock.as_raw_fd(), msgvec.as_mut_ptr(), msgvec.len() as u32, 0) };

        if ret < 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::WouldBlock { return Ok(0); }
            return Err(tquic::Error::InvalidOperation(format!("socket sendmmsg(): {:?}", err)));
        }
        Ok(ret as usize)
    }
}

struct PerfHandler {
    server: bool,
    bytes_transferred: Arc<Mutex<u64>>,
    mode: String,
    data_size: u64,
    next_send_time: Option<Instant>,
    pending_bytes: u64,           
    frame_interval: Duration,
    
    send_buffer: Vec<u8>,
    player_state: VideoPlayerState,
    stop: Rc<Cell<bool>>,
    stop_writing: Rc<Cell<bool>>,
}

impl TransportHandler for PerfHandler {
    fn on_conn_created(&mut self, _conn: &mut Connection) {
        log::info!("QUIC 连接已建立！开始测速...");
    }

    fn on_conn_established(&mut self, conn: &mut Connection) {
        if self.server {
            log::info!("QUIC 连接已建立！等待客户端发送数据...");
        } else {
            log::info!("QUIC 连接已建立！开始发送数据...");
            if self.mode != "VIDEO" {
                conn.stream_write(0, Bytes::copy_from_slice(b"START"), false).unwrap();
            }
            let _ = conn.stream_bidi_new(1, true);
        }
    }

    fn on_conn_closed(&mut self, _conn: &mut Connection) {
        info!("QUIC 连接已关闭。");
        info!("已传输 {} 字节", self.bytes_transferred.lock().unwrap());
        self.bytes_transferred.lock().unwrap().clone_from(&0);
        
        // 当收到断开连接信号且为VIDEO模式，打印最终卡顿比报告
        if self.mode == "VIDEO" && self.server {
            let total_play = self.player_state.total_play_time.as_secs_f64();
            let total_stall = self.player_state.total_stall_time.as_secs_f64();
            let ratio = if total_play + total_stall > 0.0 {
                total_stall / (total_play + total_stall)
            } else {
                0.0
            };
            println!("\n========= 视频接收端最终报告 (QoE) =========");
            println!("完整接收帧数: {}", self.player_state.frames_received);
            println!("理想播放耗时: {:.2} 秒 | 缓冲区卡顿等待耗时: {:.2} 秒", total_play, total_stall);
            println!("Buffering Ratio (卡顿比): {:.2} %", ratio * 100.0);
            println!("==========================================\n");
            let _ = std::io::Write::flush(&mut std::io::stdout());
            self.player_state.recv_buffer.clear();
            self.player_state.first_frame_time = None;
            self.player_state.expected_play_time = None;
            self.player_state.total_play_time = Duration::default();
            self.player_state.total_stall_time = Duration::default();
            self.player_state.frames_received = 0;
        }
        self.stop.set(true);
    }

    fn on_stream_created(&mut self, _conn: &mut Connection, _stream_id: u64) {}

    fn on_stream_writable(&mut self, conn: &mut Connection, stream_id: u64) {
        if self.server { return; } 
        if self.stop_writing.get() {
            let _ = conn.stream_want_write(stream_id, false);
            return;
        }

        let now = Instant::now();
        let dummy_data = [0u8; 32768]; 

        match self.mode.as_str() {
            "BULK" | "STREAM" => {
                let mut loop_count = 0;
                while let Ok(written) = conn.stream_write(stream_id, Bytes::copy_from_slice(&dummy_data), false) {
                    if written == 0 { break; }
                    *self.bytes_transferred.lock().unwrap() += written as u64;
                    loop_count += 1;
                    if loop_count > 100 { break; } 
                }
                let _ = conn.stream_want_write(stream_id, true);
            }

            "RPC" => {
                let mut rng = rand::thread_rng();
                if self.pending_bytes == 0 && (self.next_send_time.is_none() || now >= self.next_send_time.unwrap()) {
                    self.pending_bytes = rng.gen_range(10_000..1_000_000);
                    let think_time = Duration::from_millis(rng.gen_range(50..200));
                    self.next_send_time = Some(now + think_time);
                }

                if self.pending_bytes > 0 {
                    while let Ok(written) = conn.stream_write(stream_id, Bytes::copy_from_slice(&dummy_data[..std::cmp::min(32768, self.pending_bytes as usize)]), false) {
                        if written == 0 { break; }
                        self.pending_bytes -= written as u64;
                        *self.bytes_transferred.lock().unwrap() += written as u64;
                        if self.pending_bytes == 0 { break; }
                    }
                }
                let _ = conn.stream_want_write(stream_id, true);
            }

            "VIDEO" => {
                let mut rng = rand::thread_rng();
                if self.next_send_time.is_none() {
                    self.next_send_time = Some(now);
                }

                while now >= self.next_send_time.unwrap() {
                    // let frame_size = rng.gen_range(50_000..150_000) as u32;
                    let frame_size = rng.gen_range(5_000..20_883) as u32;
                    self.send_buffer.extend_from_slice(&frame_size.to_be_bytes());
                    self.send_buffer.resize(self.send_buffer.len() + frame_size as usize, 0); 
                    
                    self.next_send_time = Some(self.next_send_time.unwrap() + self.frame_interval);
                }

                while !self.send_buffer.is_empty() {
                    let chunk_size = std::cmp::min(self.send_buffer.len(), 65536);
                    match conn.stream_write(stream_id, Bytes::copy_from_slice(&self.send_buffer[..chunk_size]), false) {
                        Ok(written) => {
                            if written == 0 { break; } 
                            self.send_buffer.drain(..written);
                            *self.bytes_transferred.lock().unwrap() += written as u64;
                        },
                        Err(_) => break,
                    }
                }
                let _ = conn.stream_want_write(stream_id, true);
            }

            _ => { warn!("未知的运行模式: {}", self.mode); }
        }
    }

    fn on_stream_readable(&mut self, conn: &mut Connection, stream_id: u64) {
        if self.server {
            let mut buf = [0u8; 65536]; 
            while let Ok((read_len, fin)) = conn.stream_read(stream_id, &mut buf) {
                if read_len > 0 {
                    let mut bytes = self.bytes_transferred.lock().unwrap();
                    *bytes += read_len as u64;
                    
                    if self.mode == "VIDEO" {
                        self.player_state.recv_buffer.extend_from_slice(&buf[..read_len]);
                        self.player_state.process_frames();
                    }
                }
                
                if fin { break; }
                if read_len == 0 { break; }
            }
        }
    }

    fn on_stream_closed(&mut self, _conn: &mut Connection, _stream_id: u64) {}
    fn on_new_token(&mut self, _conn: &mut Connection, _token: Vec<u8>) {}
}

fn extract_number_from_filename(filename: &str) -> Option<i32> {
    let stem = std::path::Path::new(filename).file_stem()?.to_str()?;
    let non_digit_index = stem.rfind(|c: char| !c.is_ascii_digit());
    let num_str = match non_digit_index {
        Some(idx) => &stem[idx + 1..],
        None => stem,
    };
    if num_str.is_empty() { return None; }
    num_str.parse::<i32>().ok()
}

fn output_algorithm_name(args: &IperfOpt) -> String {
    if args.congestion_control.eq_ignore_ascii_case("llmgen") {
        let num = args
            .cca_filename
            .as_ref()
            .and_then(|f| extract_number_from_filename(f))
            .unwrap_or(-1);
        format!("{}{}", args.congestion_control, num)
    } else {
        args.congestion_control.clone()
    }
}

struct TimeSeriesLogger {
    writer: BufWriter<File>,
    algorithm: String,
    trace: String,
    last_sample_time: Instant,
    last_acked_bytes: u64,
    sample_interval: Duration,
}

impl TimeSeriesLogger {
    fn new(path: &str, args: &IperfOpt, now: Instant) -> std::io::Result<Self> {
        if let Some(parent) = Path::new(path).parent() {
            if !parent.as_os_str().is_empty() {
                create_dir_all(parent)?;
            }
        }

        let mut writer = BufWriter::new(File::create(path)?);
        writeln!(
            writer,
            "algorithm,trace,elapsed_s,interval_s,acked_bytes_total,interval_acked_bytes,throughput_kbps,srtt_ms,min_rtt_ms,rttvar_ms,cwnd_bytes,max_cwnd_bytes,min_cwnd_bytes,pacing_rate_bps,lost_bytes,lost_count,in_slow_start"
        )?;

        Ok(Self {
            writer,
            algorithm: output_algorithm_name(args),
            trace: args.trace.clone().unwrap_or("unknown".to_string()),
            last_sample_time: now,
            last_acked_bytes: 0,
            sample_interval: Duration::from_millis(args.sample_interval_ms.max(1)),
        })
    }

    fn maybe_sample(
        &mut self,
        conn: &mut Connection,
        local_addr: SocketAddr,
        remote_addr: SocketAddr,
        start_time: Instant,
        now: Instant,
        force: bool,
    ) {
        let interval = now.saturating_duration_since(self.last_sample_time);
        if !force && interval < self.sample_interval {
            return;
        }

        match conn.get_path_stats(local_addr, remote_addr) {
            Ok(p) => {
                let interval_secs = interval.as_secs_f64().max(0.000_001);
                let total_acked = p.acked_bytes as u64;
                let interval_acked = total_acked.saturating_sub(self.last_acked_bytes);
                let throughput_kbps = (interval_acked as f64 * 8.0) / (interval_secs * 1000.0);
                let elapsed_secs = now.saturating_duration_since(start_time).as_secs_f64();
                let pacing_rate_bps = p.pacing_rate.saturating_mul(8);

                if let Err(e) = writeln!(
                    self.writer,
                    "{},{},{:.8},{:.8},{},{},{:.3},{:.3},{:.3},{:.3},{},{},{},{},{},{},{}",
                    self.algorithm,
                    self.trace,
                    elapsed_secs,
                    interval_secs,
                    total_acked,
                    interval_acked,
                    throughput_kbps,
                    p.srtt as f64 / 1000.0,
                    p.min_rtt as f64 / 1000.0,
                    p.rttvar as f64 / 1000.0,
                    p.final_cwnd,
                    p.max_cwnd,
                    p.min_cwnd,
                    pacing_rate_bps,
                    p.lost_bytes,
                    p.lost_count,
                    p.in_slow_start
                ) {
                    error!("Error when writing timeseries sample: {:?}", e);
                }

                self.last_sample_time = now;
                self.last_acked_bytes = total_acked;
            }
            Err(e) => error!("Error when fetching stats for timeseries: {:?}", e),
        }
    }

    fn flush(&mut self) {
        if let Err(e) = self.writer.flush() {
            error!("Error when flushing timeseries output: {:?}", e);
        }
    }
}

fn reach_target_data(iperf: &mut Iperf, is_server: bool, target_size: u64, local_addr: SocketAddr, remote_addr: SocketAddr) -> bool {
    if target_size <= 0 { return false; }
    if !is_server {
        match iperf.endpoint.conn_get_mut(iperf.conn_id){
            Some(c) => {
                match c.get_path_stats(local_addr, remote_addr){
                    Ok(p) => return p.acked_bytes as u64 >= target_size,
                    Err(e) => {
                        error!("Error when fetching stats: {:?}", e);
                        return false;
                    }
                }
            }
            None => return false,
        }
    }
    false
}

fn main() {
    env_logger::init();
    let args = IperfOpt::parse();

    let socket = match args.server {
        true => UdpSocket::bind(args.listen).expect("绑定服务器地址失败"),
        false => UdpSocket::bind("0.0.0.0:0".parse().unwrap()).expect("绑定客户端地址失败"),
    };

    let mut iperf = Iperf::new(socket, &args);

    if !args.server {
        log::info!("TQUIC-perf 以客户端模式启动，连接地址: {:?}", args.connect_to);
        if let Some(target_ip) = args.connect_to {
            match iperf.endpoint.connect(iperf.sender.local_addr, target_ip, None, None, None, None){
                Ok(idx) => {
                    log::info!("正在连接服务器 {}...", target_ip);
                    if let Err(e) = iperf.endpoint.process_connections() {
                        error!("process connections error: {:?}", e);
                    }
                    iperf.conn_id = idx;
                }
                Err(e) => {
                    error!("连接服务器失败: {:?}", e);
                    return;
                }
            }
        }
    }

    info!("TQUIC-perf 启动运行...");

    let duration = Duration::from_secs(args.duration);
    let start_time = Instant::now();
    let local_addr = iperf.sender.local_addr.clone();
    let mut events = mio::Events::with_capacity(128);
    let mut timeseries_logger = if !args.server {
        match args.timeseries_output.as_ref() {
            Some(path) => Some(
                TimeSeriesLogger::new(path, &args, start_time)
                    .expect("failed to create timeseries output"),
            ),
            None => None,
        }
    } else {
        None
    };

    loop {
        if !args.server && !iperf.stop_writing.get() && (start_time.elapsed() >= duration || reach_target_data(&mut iperf, args.server, args.data_size, local_addr, args.connect_to.unwrap())) {
            let time_to_now = start_time.elapsed().as_micros() as f64 / 1000000.0;
            info!("测试完成，持续时间: {:?}秒", time_to_now);
            match iperf.endpoint.conn_get_mut(iperf.conn_id) {
                Some(c) => {
                    match c.get_path_stats(iperf.sender.local_addr, args.connect_to.unwrap()){
                        Ok(p) => {
                            if args.congestion_control.eq_ignore_ascii_case("llmgen") {
                                let num = args.cca_filename.as_ref().and_then(|f| extract_number_from_filename(f)).unwrap_or(-1);
                                println!("{}{},{},{:.8},{:.3},{:.3},{:.3},{:.3}", 
                                    args.congestion_control, num, args.trace.clone().unwrap_or("unknown".to_string()),
                                    time_to_now, (p.acked_bytes as f64 * 8.0) / (time_to_now * 1000.0), 
                                    p.srtt as f64 / 1000.0, p.min_rtt as f64 / 1000.0, p.rttvar as f64 / 1000.0,
                                );
                            } else {
                                println!("{},{},{:.8},{:.3},{:.3},{:.3},{:.3}", 
                                    args.congestion_control, args.trace.clone().unwrap_or("unknown".to_string()),
                                    time_to_now, (p.acked_bytes as f64 * 8.0) / (time_to_now * 1000.0), 
                                    p.srtt as f64 / 1000.0, p.min_rtt as f64 / 1000.0, p.rttvar as f64 / 1000.0,
                                );
                            }
                        }
                        Err(e) => error!("Error when fetching stats: {:?}", e),
                    }
                    if let Some(logger) = timeseries_logger.as_mut() {
                        logger.maybe_sample(
                            c,
                            iperf.sender.local_addr,
                            args.connect_to.unwrap(),
                            start_time,
                            Instant::now(),
                            true,
                        );
                        logger.flush();
                    }
                    // 断开连接，给对端发送 CONNECTION_CLOSE 报文
                    let _ = c.close(true, 0, "Done".as_bytes());
                }
                None => info!("Connection doesn't exist"),
            }
            iperf.stop_writing.set(true);
        }

        if iperf.stop.get() && !args.server {
            break; // 客户端确认连接关闭后退出
        }

        if let Err(e) = iperf.endpoint.process_connections() {
            error!("process connections error: {:?}", e);
        }

        if !args.server {
            if let Some(logger) = timeseries_logger.as_mut() {
                if let Some(c) = iperf.endpoint.conn_get_mut(iperf.conn_id) {
                    logger.maybe_sample(
                        c,
                        iperf.sender.local_addr,
                        args.connect_to.unwrap(),
                        start_time,
                        Instant::now(),
                        false,
                    );
                }
            }
        }

        let mut max_wait = Duration::from_millis(100);
        if !args.server && args.mode.to_uppercase() == "VIDEO" {
            max_wait = Duration::from_millis(5);
        }
        
        let timeout = match iperf.endpoint.timeout() {
            Some(t) => std::cmp::min(t, max_wait),
            None => max_wait,
        };

        match iperf.poll.poll(&mut events, Some(timeout)) {
            Ok(_) => {}
            Err(e) => {
                error!("poll error: {:?}", e);
                continue;
            }
        }

        for event in events.iter() {
            if event.is_readable() {
                iperf.process_read_event(event).unwrap_or_else(|e| {
                    error!("处理读事件失败: {:?}", e);
                });
            }
        }

        iperf.endpoint.on_timeout(Instant::now());
    }
}
