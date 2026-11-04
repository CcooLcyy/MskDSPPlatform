use super::*;
use axum::body::{Body, Bytes};
use prost::Message;
use std::convert::Infallible;
use tempfile::TempDir;
use tonic::codec::{Codec, ProstCodec};

type DeviceInput = mpsc::Sender<Result<Bytes, Infallible>>;
type ControlOutput = mpsc::Receiver<Result<PlatformMessage, Status>>;

async fn control_session() -> (
    TempDir,
    DeviceInput,
    ControlOutput,
    tokio::task::JoinHandle<(Result<(), Status>, tokio::time::Instant)>,
) {
    // 存储初始化在暂停时钟前完成，虚拟期限内不执行数据库或网络 I/O。
    let dir = tempfile::tempdir().unwrap();
    let store = DeviceStore::open(dir.path().join("devices.sqlite3"))
        .await
        .unwrap();
    let accounts = crate::accounts::AccountStore::open(dir.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let service = RemoteService::new(store, ClientAuth::new(accounts, 3600), Hub::default());
    let (input, received) = mpsc::channel(8);
    let body = Body::from_stream(ReceiverStream::new(received));
    let mut codec = ProstCodec::<DeviceMessage, DeviceMessage>::default();
    let mut inbound = Streaming::new_request(codec.decoder(), body, None, None);
    let (outbound, output) = mpsc::channel(1);
    tokio::time::pause();
    let task = tokio::spawn(async move {
        let result = service
            .control(
                "field",
                "test-connection",
                &mut inbound,
                &outbound,
                &CancellationToken::new(),
            )
            .await;
        (result, tokio::time::Instant::now())
    });
    (dir, input, output, task)
}

async fn assert_welcome(output: &mut ControlOutput) {
    let message = output.recv().await.unwrap().unwrap();
    let Some(platform_message::Payload::Welcome(welcome)) = message.payload else {
        panic!("首条控制响应必须是 Welcome");
    };
    assert_eq!(welcome.heartbeat_interval_seconds, 5);
    // 让控制循环建立失联计时器，再推进时间。
    tokio::task::yield_now().await;
}

fn heartbeat() -> Result<Bytes, Infallible> {
    let message = DeviceMessage {
        payload: Some(device_message::Payload::Heartbeat(DeviceHeartbeat {
            services: vec!["ModuleManager".into()],
        })),
    }
    .encode_to_vec();
    let mut frame = vec![0];
    frame.extend_from_slice(&(message.len() as u32).to_be_bytes());
    frame.extend_from_slice(&message);
    Ok(Bytes::from(frame))
}

fn assert_heartbeat_expired_at_deadline(
    result: (Result<(), Status>, tokio::time::Instant),
    last_heartbeat: tokio::time::Instant,
) {
    let (result, finished) = result;
    let elapsed = finished.duration_since(last_heartbeat);
    // Tokio 1.53.1 将期限向上取整至一毫秒 tick；仅允许此分辨率，不放宽失联窗口。
    assert!(elapsed >= Duration::from_secs(20), "不应在二十秒前判定失联");
    assert!(
        elapsed <= Duration::from_secs(20) + Duration::from_millis(1),
        "失联期限只能包含一毫秒定时器取整误差，实际为 {elapsed:?}"
    );
    let error = result.unwrap_err();
    assert_eq!(error.code(), tonic::Code::DeadlineExceeded);
    assert_eq!(error.message(), "设备心跳超时");
}

// 下发五秒心跳，静默控制流在二十秒之前保持、达到期限时关闭。
#[tokio::test]
async fn silent_device_expires_after_twenty_seconds() {
    let (_dir, _input, mut output, task) = control_session().await;
    assert_welcome(&mut output).await;
    let started = tokio::time::Instant::now();
    tokio::time::advance(Duration::from_secs(19)).await;
    tokio::task::yield_now().await;
    assert!(!task.is_finished(), "未到二十秒时不应判定设备失联");
    tokio::time::advance(Duration::from_secs(1)).await;
    assert_heartbeat_expired_at_deadline(task.await.unwrap(), started);
}

// 有效心跳重置二十秒期限，既不能沿用旧期限提前断线，也不能永久保持在线。
#[tokio::test]
async fn valid_heartbeat_restarts_twenty_second_deadline() {
    let (_dir, input, mut output, task) = control_session().await;
    assert_welcome(&mut output).await;
    tokio::time::advance(Duration::from_secs(19)).await;
    input.send(heartbeat()).await.unwrap();
    assert!(matches!(
        output.recv().await.unwrap().unwrap().payload,
        Some(platform_message::Payload::HeartbeatAck(_))
    ));
    let last_heartbeat = tokio::time::Instant::now();
    tokio::time::advance(Duration::from_secs(19)).await;
    tokio::task::yield_now().await;
    assert!(!task.is_finished(), "有效心跳之后应重新计算失联期限");
    tokio::time::advance(Duration::from_secs(1)).await;
    assert_heartbeat_expired_at_deadline(task.await.unwrap(), last_heartbeat);
}

// 心跳响应队列已满时，也必须遵守二十秒失联期限，不能等待通用的三十秒写入期限。
#[tokio::test]
async fn blocked_heartbeat_ack_does_not_extend_device_deadline() {
    let (_dir, input, mut output, task) = control_session().await;
    assert_welcome(&mut output).await;
    input.send(heartbeat()).await.unwrap();
    for _ in 0..32 {
        if output.len() == 1 {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(output.len(), 1, "第一次心跳响应应填满控制队列");
    let last_heartbeat = tokio::time::Instant::now();
    input.send(heartbeat()).await.unwrap();
    for _ in 0..32 {
        if input.capacity() == input.max_capacity() {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(input.capacity(), input.max_capacity(), "第二次心跳应已读取");
    tokio::time::advance(Duration::from_secs(19)).await;
    tokio::task::yield_now().await;
    assert!(!task.is_finished(), "背压不应提前判定失联");
    tokio::time::advance(Duration::from_secs(1)).await;
    // advance 和单次 yield 不保证定时器已被轮询；等待任务并检查任务内记录的完成时刻。
    // 若遗漏控制流的二十秒约束而退回三十秒写入期限，完成时刻断言仍会失败。
    assert_heartbeat_expired_at_deadline(task.await.unwrap(), last_heartbeat);
}
