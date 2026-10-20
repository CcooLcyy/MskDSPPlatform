//! 隧道数据面：两方向独立有界转发，背压不会阻止反方向收发。
use super::{
    hub::{FrameSender, Peer},
    proto::*,
};
use std::time::Duration;
use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;
use tonic::{Status, Streaming};
const BLOCK: Duration = Duration::from_secs(30);
pub async fn send<T: Send>(
    tx: &mpsc::Sender<Result<T, Status>>,
    value: T,
    cancel: &CancellationToken,
) -> Result<(), Status> {
    tokio::select! {
     biased;
     _=cancel.cancelled()=>Err(Status::unavailable("设备控制连接已断开")),
     result=tokio::time::timeout(BLOCK,tx.send(Ok(value)))=>{
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
    loop {
        let input = inbound.message().await?;
        let eof = input.is_none();
        let was_half = half;
        let closed = forward(input, &mut half, outbound, cancel, id, direction).await?;
        if !was_half && half {
            let _ = half_notify.send(()).await;
        }
        if closed {
            return Ok(true);
        }
        if eof {
            return Ok(false);
        }
    }
}
async fn forward(
    input: Option<TunnelFrame>,
    half: &mut bool,
    target: &FrameSender,
    cancel: &CancellationToken,
    id: &str,
    direction: &str,
) -> Result<bool, Status> {
    let Some(input) = input else {
        if !*half {
            *half = true;
            send(
                target,
                TunnelFrame {
                    payload: Some(tunnel_frame::Payload::HalfClose(TunnelHalfClose {})),
                },
                cancel,
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
            tracing::debug!(tunnel_id=%id,direction,bytes=data.len(),"转发隧道数据");
        }
        Some(tunnel_frame::Payload::HalfClose(_)) => {
            if *half {
                return Err(Status::invalid_argument("隧道不允许重复半关闭"));
            }
            *half = true;
        }
        Some(tunnel_frame::Payload::Close(_)) => {
            send(
                target,
                TunnelFrame {
                    payload: Some(tunnel_frame::Payload::Close(TunnelClose {
                        message: "远端已关闭隧道".into(),
                    })),
                },
                cancel,
            )
            .await?;
            return Ok(true);
        }
        _ => return Err(Status::invalid_argument("隧道建立后收到非法帧")),
    }
    send(target, input, cancel).await?;
    Ok(false)
}
