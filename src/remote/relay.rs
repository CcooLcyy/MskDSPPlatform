//! 隧道数据面：两方向独立有界转发，背压不会阻止反方向收发。
use super::{
    hub::{FrameSender, Peer},
    proto::*,
};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tonic::{Status, Streaming};
const BLOCK: Duration = Duration::from_secs(30);
const PROGRESS_BYTES: u64 = 2 * 1024 * 1024;

struct DirectionProgress<'a> {
    id: &'a str,
    direction: &'a str,
    started: Instant,
    last_progress: Instant,
    bytes: u64,
    frames: u64,
    reported_bytes: u64,
    end_reason: &'static str,
}

impl<'a> DirectionProgress<'a> {
    fn new(id: &'a str, direction: &'a str, target: &FrameSender) -> Self {
        tracing::info!(tunnel_id=%id, direction, queue_capacity=target.max_capacity(), "隧道方向开始转发");
        let now = Instant::now();
        Self {
            id,
            direction,
            started: now,
            last_progress: now,
            bytes: 0,
            frames: 0,
            reported_bytes: 0,
            end_reason: "隧道任务或另一方向已结束",
        }
    }

    async fn send_with_timeout(
        &mut self,
        target: &FrameSender,
        input: TunnelFrame,
        cancel: &CancellationToken,
        timeout: Duration,
    ) -> Result<(), Status> {
        let bytes = match &input.payload {
            Some(tunnel_frame::Payload::Data(data)) => data.len() as u64,
            _ => 0,
        };
        let waiting = Instant::now();
        if let Err(error) = send_with_timeout(target, input, cancel, timeout).await {
            self.end_reason = "发送失败";
            let waited_ms = waiting.elapsed().as_millis() as u64;
            tracing::warn!(tunnel_id=%self.id, direction=self.direction,
                transferred_bytes=self.bytes, transferred_frames=self.frames, pending_bytes=bytes,
                waited_ms, queue_remaining=target.capacity(),
                last_progress_ms=self.last_progress.elapsed().as_millis() as u64,
                code=?error.code(), "隧道方向发送失败");
            return Err(if error.code() == tonic::Code::DeadlineExceeded {
                Status::deadline_exceeded(format!(
                    "隧道响应阻塞超时：{}，已转发 {} 字节，等待 {} 毫秒",
                    self.direction, self.bytes, waited_ms
                ))
            } else {
                error
            });
        }
        self.bytes += bytes;
        self.frames += 1;
        self.last_progress = Instant::now();
        if self.bytes - self.reported_bytes >= PROGRESS_BYTES {
            self.reported_bytes = self.bytes;
            tracing::info!(tunnel_id=%self.id, direction=self.direction,
                transferred_bytes=self.bytes, transferred_frames=self.frames,
                elapsed_ms=self.started.elapsed().as_millis() as u64,
                queue_remaining=target.capacity(), "隧道方向转发进展");
        }
        Ok(())
    }
}

impl Drop for DirectionProgress<'_> {
    fn drop(&mut self) {
        tracing::info!(tunnel_id=%self.id, direction=self.direction,
            transferred_bytes=self.bytes, transferred_frames=self.frames,
            elapsed_ms=self.started.elapsed().as_millis() as u64,
            last_progress_ms=self.last_progress.elapsed().as_millis() as u64,
            reason=self.end_reason, "隧道方向转发结束");
    }
}

pub async fn send<T: Send>(
    tx: &mpsc::Sender<Result<T, Status>>,
    value: T,
    cancel: &CancellationToken,
) -> Result<(), Status> {
    send_with_timeout(tx, value, cancel, BLOCK).await
}

async fn send_with_timeout<T: Send>(
    tx: &mpsc::Sender<Result<T, Status>>,
    value: T,
    cancel: &CancellationToken,
    timeout: Duration,
) -> Result<(), Status> {
    tokio::select! {
     biased;
     _=cancel.cancelled()=>Err(Status::unavailable("设备控制连接已断开")),
     result=tokio::time::timeout(timeout,tx.send(Ok(value)))=>{
      result.map_err(|_|Status::deadline_exceeded("隧道响应阻塞超时"))?
       .map_err(|_|Status::cancelled("接收端已关闭"))
     }
    }
}
pub async fn run(
    id: &str,
    upper: Streaming<TunnelFrame>,
    outbound: &FrameSender,
    peer: Peer,
    cancel: &CancellationToken,
) -> Result<(), Status> {
    send(
        outbound,
        TunnelFrame {
            payload: Some(tunnel_frame::Payload::Ready(TunnelReady {})),
        },
        cancel,
    )
    .await?;
    send(
        &peer.outbound,
        TunnelFrame {
            payload: Some(tunnel_frame::Payload::Ready(TunnelReady {})),
        },
        cancel,
    )
    .await?;
    let (half_tx, mut half_rx) = mpsc::channel(2);
    let upward = pump(
        peer.inbound,
        outbound,
        cancel,
        id,
        "下位机至上位机",
        half_tx.clone(),
    );
    let downward = pump(upper, &peer.outbound, cancel, id, "上位机至下位机", half_tx);
    tokio::pin!(upward, downward);
    let (mut upward_done, mut downward_done, mut half_count) = (false, false, 0);
    let result = loop {
        tokio::select! {
         _=cancel.cancelled()=>break Err(Status::unavailable("设备控制连接已断开")),
         _=outbound.closed()=>break Err(Status::cancelled("上位机已关闭隧道")),
         _=peer.outbound.closed()=>break Err(Status::cancelled("下位机已关闭隧道")),
         result=&mut upward,if !upward_done=>{
          match result {Err(error)=>break Err(error),Ok(true)=>break Ok(()),Ok(false)=>upward_done=true}
         }
         result=&mut downward,if !downward_done=>{
          match result {Err(error)=>break Err(error),Ok(true)=>break Ok(()),Ok(false)=>downward_done=true}
         }
         half=half_rx.recv(),if half_count<2=>{
          if half.is_some(){half_count+=1;}
          if half_count==2{break Ok(())}
         }
        }
        if upward_done && downward_done {
            break Ok(());
        }
    };
    if let Err(error) = &result {
        let _ = peer
            .outbound
            .try_send(Err(Status::new(error.code(), error.message().to_owned())));
    }
    result
}
async fn pump(
    mut inbound: Streaming<TunnelFrame>,
    outbound: &FrameSender,
    cancel: &CancellationToken,
    id: &str,
    direction: &str,
    half_notify: mpsc::Sender<()>,
) -> Result<bool, Status> {
    let mut half = false;
    let mut progress = DirectionProgress::new(id, direction, outbound);
    loop {
        let input = match inbound.message().await {
            Ok(input) => input,
            Err(error) => {
                progress.end_reason = "接收失败";
                tracing::warn!(tunnel_id=%id, direction, code=?error.code(), "隧道方向接收失败");
                return Err(error);
            }
        };
        let eof = input.is_none();
        let was_half = half;
        let closed = match forward(input, &mut half, outbound, cancel, &mut progress).await {
            Ok(closed) => closed,
            Err(error) => {
                if progress.end_reason != "发送失败" {
                    progress.end_reason = "帧校验失败";
                    tracing::warn!(tunnel_id=%id, direction, code=?error.code(), "隧道方向帧校验失败");
                }
                return Err(error);
            }
        };
        if !was_half && half {
            let _ = half_notify.send(()).await;
        }
        if closed {
            progress.end_reason = "收到关闭帧";
            return Ok(true);
        }
        if eof {
            progress.end_reason = "输入流已结束";
            return Ok(false);
        }
    }
}
async fn forward(
    input: Option<TunnelFrame>,
    half: &mut bool,
    target: &FrameSender,
    cancel: &CancellationToken,
    progress: &mut DirectionProgress<'_>,
) -> Result<bool, Status> {
    let Some(input) = input else {
        if !*half {
            *half = true;
            progress
                .send_with_timeout(
                    target,
                    TunnelFrame {
                        payload: Some(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})),
                    },
                    cancel,
                    BLOCK,
                )
                .await?;
        }
        return Ok(false);
    };
    match &input.payload {
        Some(tunnel_frame::Payload::Data(data)) => {
            if *half || data.is_empty() || data.len() > 65536 {
                return Err(Status::invalid_argument(
                    "隧道数据为空、超过 64 KiB 或已半关闭",
                ));
            }
        }
        Some(tunnel_frame::Payload::HalfClose(_)) => {
            if *half {
                return Err(Status::invalid_argument("隧道不允许重复半关闭"));
            }
            *half = true;
        }
        Some(tunnel_frame::Payload::Close(_)) => {
            progress
                .send_with_timeout(
                    target,
                    TunnelFrame {
                        payload: Some(tunnel_frame::Payload::Close(TunnelClose {
                            message: "远端已关闭隧道".into(),
                        })),
                    },
                    cancel,
                    BLOCK,
                )
                .await?;
            return Ok(true);
        }
        _ => return Err(Status::invalid_argument("隧道建立后收到非法帧")),
    }
    progress
        .send_with_timeout(target, input, cancel, BLOCK)
        .await?;
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    // 累计量仅统计成功入队的数据，满队列超时保留方向及已有累计量。
    #[tokio::test]
    async fn blocked_frame_reports_direction_without_counting_unsent_bytes() {
        let (tx, _rx) = mpsc::channel(1);
        let cancel = CancellationToken::new();
        let mut progress = DirectionProgress::new("测试隧道", "上位机至下位机", &tx);
        progress
            .send_with_timeout(
                &tx,
                TunnelFrame {
                    payload: Some(tunnel_frame::Payload::Data(vec![1; 17])),
                },
                &cancel,
                BLOCK,
            )
            .await
            .unwrap();
        let error = progress
            .send_with_timeout(
                &tx,
                TunnelFrame {
                    payload: Some(tunnel_frame::Payload::Data(vec![2; 31])),
                },
                &cancel,
                Duration::from_millis(20),
            )
            .await
            .unwrap_err();
        assert_eq!(error.code(), tonic::Code::DeadlineExceeded);
        assert!(error.message().contains("上位机至下位机"));
        assert!(error.message().contains("17"));
        assert_eq!(progress.bytes, 17);
        assert_eq!(progress.frames, 1);
        assert_eq!(progress.end_reason, "发送失败");
    }

    // 控制连接取消时满队列写入立即退出，且不计未发送的字节。
    #[tokio::test]
    async fn cancelled_frame_preserves_successful_progress() {
        let (tx, _rx) = mpsc::channel(1);
        let cancel = CancellationToken::new();
        let mut progress = DirectionProgress::new("测试隧道", "下位机至上位机", &tx);
        tx.send(Ok(TunnelFrame {
            payload: Some(tunnel_frame::Payload::Ready(TunnelReady {})),
        }))
        .await
        .unwrap();
        cancel.cancel();
        let error = progress
            .send_with_timeout(
                &tx,
                TunnelFrame {
                    payload: Some(tunnel_frame::Payload::Data(vec![3; 7])),
                },
                &cancel,
                BLOCK,
            )
            .await
            .unwrap_err();
        assert_eq!(error.code(), tonic::Code::Unavailable);
        assert_eq!(progress.bytes, 0);
        assert_eq!(progress.frames, 0);
    }
}
