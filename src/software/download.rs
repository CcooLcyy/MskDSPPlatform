use super::{
    grpc::{proto, store_status},
    models::{DownloadFile, StoreError},
    store::SoftwareStore,
};
use sha2::{Digest, Sha256};
use std::{future::Future, io, time::Duration};
use tokio::{
    fs::File,
    io::{AsyncReadExt, AsyncSeekExt},
    sync::mpsc,
    time::{Instant, timeout_at},
};
use tokio_stream::wrappers::ReceiverStream;
use tonic::{Response, Status};
use tracing::{info, warn};

const DOWNLOAD_CHUNK_BYTES: usize = 256 * 1024;

#[derive(Clone, Copy)]
struct DownloadLimits {
    idle: Duration,
    total: Duration,
}

impl Default for DownloadLimits {
    fn default() -> Self {
        Self {
            idle: Duration::from_secs(60),
            total: Duration::from_secs(30 * 60),
        }
    }
}

type DownloadStream = ReceiverStream<Result<proto::DownloadPackageChunk, Status>>;

fn next_deadline(deadline: Instant, limits: DownloadLimits) -> Instant {
    deadline.min(Instant::now() + limits.idle)
}

async fn limited_io<T>(
    deadline: Instant,
    limits: DownloadLimits,
    operation: impl Future<Output = io::Result<T>>,
) -> Result<T, Status> {
    timeout_at(next_deadline(deadline, limits), operation)
        .await
        .map_err(|_| Status::deadline_exceeded("安装包读取空闲或请求总时限已到"))?
        .map_err(|error| {
            warn!(error = %error, "安装包 gRPC 下载读取失败");
            Status::data_loss("安装包文件不可读或不完整")
        })
}

// 在同一文件句柄上检查完整身份后回到请求偏移，不持有软件库数据库锁。
async fn verify_file(
    file: &mut File,
    descriptor: &DownloadFile,
    offset: u64,
    deadline: Instant,
    limits: DownloadLimits,
) -> Result<(), Status> {
    let metadata = limited_io(deadline, limits, file.metadata()).await?;
    if !metadata.is_file() || metadata.len() != descriptor.size {
        return Err(Status::data_loss("安装包文件大小或类型不正确"));
    }
    let mut buffer = vec![0; DOWNLOAD_CHUNK_BYTES];
    let mut digest = Sha256::new();
    let mut received = 0;
    loop {
        let count = limited_io(deadline, limits, file.read(&mut buffer)).await?;
        if count == 0 {
            break;
        }
        received += count as u64;
        if received > descriptor.size {
            return Err(Status::data_loss("安装包文件长度超过已登记大小"));
        }
        digest.update(&buffer[..count]);
    }
    if received != descriptor.size || format!("{:x}", digest.finalize()) != descriptor.sha256 {
        return Err(Status::data_loss("安装包文件大小或 SHA256 校验失败"));
    }
    limited_io(deadline, limits, file.seek(io::SeekFrom::Start(offset))).await?;
    Ok(())
}

// 有界队列限制读超前；接收端取消时不再等待磁盘或发送背压。
async fn pump_file(
    mut file: File,
    descriptor: DownloadFile,
    mut offset: u64,
    sender: &mpsc::Sender<Result<proto::DownloadPackageChunk, Status>>,
    deadline: Instant,
    limits: DownloadLimits,
) -> Result<(), Status> {
    loop {
        if sender.is_closed() {
            return Ok(());
        }
        let count = (descriptor.size - offset).min(DOWNLOAD_CHUNK_BYTES as u64) as usize;
        let mut data = vec![0; count];
        if count > 0 {
            tokio::select! {
                _ = sender.closed() => return Ok(()),
                result = limited_io(deadline, limits, file.read_exact(&mut data)) => { result?; }
            }
        }
        let eof = offset + count as u64 == descriptor.size;
        if eof {
            let metadata = limited_io(deadline, limits, file.metadata()).await?;
            if metadata.len() != descriptor.size {
                return Err(Status::data_loss("安装包在下载期间长度发生变化"));
            }
        }
        let chunk = proto::DownloadPackageChunk {
            offset,
            data,
            total_size: descriptor.size,
            sha256: descriptor.sha256.clone(),
            eof,
        };
        match timeout_at(next_deadline(deadline, limits), sender.send(Ok(chunk))).await {
            Ok(Ok(())) => {}
            Ok(Err(_)) => return Ok(()),
            Err(_) => {
                return Err(Status::deadline_exceeded(
                    "安装包下载接收空闲或请求总时限已到",
                ));
            }
        }
        if eof {
            return Ok(());
        }
        offset += count as u64;
    }
}

pub(super) async fn download(
    store: &SoftwareStore,
    request: proto::DownloadPackageRequest,
) -> Result<Response<DownloadStream>, Status> {
    let limits = DownloadLimits::default();
    let deadline = Instant::now() + limits.total;
    let package = timeout_at(deadline, store.get_package(&request.package_id))
        .await
        .map_err(|_| Status::deadline_exceeded("查询安装包超过请求总时限"))?
        .map_err(store_status)?;
    let descriptor = timeout_at(
        deadline,
        store.download_file(&request.package_id, &package.metadata.file_name),
    )
    .await
    .map_err(|_| Status::deadline_exceeded("检查安装包超过请求总时限"))?
    .map_err(|error| match error {
        StoreError::Storage(_) => Status::data_loss("安装包文件不可读或不完整"),
        error => store_status(error),
    })?;
    if request.offset > descriptor.size {
        return Err(Status::out_of_range("安装包下载偏移超过文件大小"));
    }
    let mut file = limited_io(deadline, limits, File::open(&descriptor.path)).await?;
    verify_file(&mut file, &descriptor, request.offset, deadline, limits)
        .await
        .map_err(|error| {
            warn!(package_id = %request.package_id, code = ?error.code(), "gRPC 安装包下载校验失败");
            error
        })?;
    let (sender, receiver) = mpsc::channel(2);
    info!(package_id = %request.package_id, offset = request.offset, size = descriptor.size, "开始 gRPC 安装包下载");
    tokio::spawn(async move {
        let result = pump_file(file, descriptor, request.offset, &sender, deadline, limits).await;
        if let Err(error) = result {
            warn!(package_id = %request.package_id, code = ?error.code(), "gRPC 安装包下载中断");
            // 取消或背压超时时不再延长任务；队列已满则关闭为无 EOF 的不完整流。
            let _ = sender.try_send(Err(error));
        } else {
            info!(package_id = %request.package_id, cancelled = sender.is_closed(), "结束 gRPC 安装包下载");
        }
    });
    Ok(Response::new(ReceiverStream::new(receiver)))
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn input() -> (tempfile::TempDir, File, DownloadFile) {
        let directory = tempfile::tempdir().unwrap();
        let data = vec![7; 4 * DOWNLOAD_CHUNK_BYTES];
        let path = directory.path().join("package.bin");
        tokio::fs::write(&path, &data).await.unwrap();
        let file = File::open(&path).await.unwrap();
        let descriptor = DownloadFile {
            path,
            size: data.len() as u64,
            sha256: format!("{:x}", Sha256::digest(&data)),
            file_name: "package.bin".into(),
        };
        (directory, file, descriptor)
    }

    // 接收端读取首块后取消，即使生产者被有界队列阻塞也必须结束。
    #[tokio::test]
    async fn grpc_download_producer_stops_when_receiver_is_cancelled() {
        let (_directory, file, descriptor) = input().await;
        let (sender, mut receiver) = mpsc::channel(1);
        let task = tokio::spawn(async move {
            pump_file(
                file,
                descriptor,
                0,
                &sender,
                Instant::now() + Duration::from_secs(10),
                DownloadLimits::default(),
            )
            .await
        });
        receiver.recv().await.unwrap().unwrap();
        drop(receiver);
        assert!(
            tokio::time::timeout(Duration::from_secs(1), task)
                .await
                .unwrap()
                .unwrap()
                .is_ok()
        );
    }

    // 不消费下载块触发有界发送背压，空闲时限与总时限均能终止后台生产者。
    #[tokio::test]
    async fn grpc_download_producer_limits_idle_and_total_time_under_backpressure() {
        for limits in [
            DownloadLimits {
                idle: Duration::from_millis(20),
                total: Duration::from_secs(5),
            },
            DownloadLimits {
                idle: Duration::from_secs(5),
                total: Duration::from_millis(20),
            },
        ] {
            let (_directory, file, descriptor) = input().await;
            let (sender, _receiver) = mpsc::channel(1);
            let result = tokio::time::timeout(
                Duration::from_secs(1),
                pump_file(
                    file,
                    descriptor,
                    0,
                    &sender,
                    Instant::now() + limits.total,
                    limits,
                ),
            )
            .await
            .unwrap();
            assert_eq!(result.unwrap_err().code(), tonic::Code::DeadlineExceeded);
        }
    }
}
