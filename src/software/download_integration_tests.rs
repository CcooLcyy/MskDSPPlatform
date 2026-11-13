use super::{
    grpc::{CatalogGrpcService, SoftwareCatalogServiceServer, proto},
    models::{Package, PackageMetadata},
    store::SoftwareStore,
};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Code, transport::Server};

type Client =
    proto::software_catalog_service_client::SoftwareCatalogServiceClient<tonic::transport::Channel>;

struct RunningServer(tokio::task::JoinHandle<()>);
impl Drop for RunningServer {
    fn drop(&mut self) {
        self.0.abort();
    }
}

async fn client(store: SoftwareStore) -> (Client, RunningServer) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let task = tokio::spawn(async move {
        Server::builder()
            .add_service(SoftwareCatalogServiceServer::new(CatalogGrpcService::new(
                store,
            )))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .unwrap();
    });
    let connection = Client::connect(format!("http://{address}")).await.unwrap();
    (connection, RunningServer(task))
}

fn metadata(product: &str, bytes: &[u8]) -> PackageMetadata {
    PackageMetadata {
        product: product.into(),
        version: "1.0.0".into(),
        platform: if product == "mskdsp-upper" {
            "windows-x86_64-nsis".into()
        } else {
            "linux-arm64".into()
        },
        file_name: "installer.bin".into(),
        size: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
        signature: "test-signature".into(),
        package_version: "1.0.0".into(),
        image_id: format!("sha256:{}", "b".repeat(64)),
        ..Default::default()
    }
}

async fn upload(store: &SoftwareStore, metadata: PackageMetadata, bytes: &[u8]) -> Package {
    let task = store.create_upload(metadata).await.unwrap();
    for (index, chunk) in bytes.chunks(512 * 1024).enumerate() {
        store
            .append_chunk(&task.id, (index * 512 * 1024) as u64, chunk.to_vec())
            .await
            .unwrap();
    }
    store.complete_upload(&task.id).await.unwrap()
}

async fn collect(client: &mut Client, package: &Package, offset: u64) -> Vec<u8> {
    let mut stream = client
        .download_package(proto::DownloadPackageRequest {
            package_id: package.id.clone(),
            offset,
        })
        .await
        .unwrap()
        .into_inner();
    let mut current = offset;
    let mut bytes = Vec::new();
    let mut eof = false;
    while let Some(chunk) = stream.message().await.unwrap() {
        assert!(!eof, "EOF 后不能再发送数据块");
        assert_eq!(chunk.offset, current);
        assert_eq!(chunk.total_size, package.metadata.size);
        assert_eq!(chunk.sha256, package.metadata.sha256);
        assert!(chunk.data.len() <= 256 * 1024);
        assert!(!chunk.data.is_empty() || chunk.eof);
        current += chunk.data.len() as u64;
        bytes.extend(chunk.data);
        eof = chunk.eof;
    }
    assert!(eof, "流结束必须有显式 EOF");
    assert_eq!(current, package.metadata.size);
    bytes
}

// 真实 Tonic 多块下载上下位机包，取消后按实际首块偏移续传，完整字节与摘要保持一致。
#[tokio::test]
async fn grpc_download_streams_upper_and_lower_and_resumes_after_interruption() {
    for product in ["mskdsp-upper", "mskdsp-lower"] {
        let directory = tempfile::tempdir().unwrap();
        let data: Vec<u8> = (0..(3 * 256 * 1024 + 123))
            .map(|i| (i % 251) as u8)
            .collect();
        let store = SoftwareStore::new(directory.path().into(), 2 * 1024 * 1024).unwrap();
        let package = upload(&store, metadata(product, &data), &data).await;
        store.publish(&package.id, "ci").await.unwrap();
        let (mut client, _running) = client(store).await;
        assert_eq!(collect(&mut client, &package, 0).await, data);
        let mut interrupted = client
            .download_package(proto::DownloadPackageRequest {
                package_id: package.id.clone(),
                offset: 0,
            })
            .await
            .unwrap()
            .into_inner();
        let first = interrupted.message().await.unwrap().unwrap();
        assert!(!first.eof);
        assert_eq!(first.data.len(), 256 * 1024);
        drop(interrupted);
        let mut resumed = first.data;
        resumed.extend(collect(&mut client, &package, resumed.len() as u64).await);
        assert_eq!(resumed, data);
        assert_eq!(
            format!("{:x}", Sha256::digest(&resumed)),
            package.metadata.sha256
        );
    }
}

// 文件尾返回唯一空 EOF，任意合法字节偏移可续传，越界必须明确拒绝。
#[tokio::test]
async fn grpc_download_tail_offset_and_out_of_range_are_explicit() {
    let directory = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
    let data = b"package-content";
    let package = upload(&store, metadata("mskdsp-upper", data), data).await;
    store.publish(&package.id, "ci").await.unwrap();
    let (mut client, _running) = client(store).await;
    assert!(
        collect(&mut client, &package, data.len() as u64)
            .await
            .is_empty()
    );
    assert_eq!(collect(&mut client, &package, 3).await, &data[3..]);
    assert_eq!(
        client
            .download_package(proto::DownloadPackageRequest {
                package_id: package.id,
                offset: data.len() as u64 + 1,
            })
            .await
            .unwrap_err()
            .code(),
        Code::OutOfRange
    );
}

// 未发布和平台自身候选不可公开读取；已发布后撤回仅移除通道，历史原始文件仍可下载。
#[tokio::test]
async fn grpc_download_enforces_public_boundary_and_keeps_withdrawn_history() {
    let directory = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
    let data = b"candidate";
    let upper = upload(&store, metadata("mskdsp-upper", data), data).await;
    let platform = upload(
        &store,
        PackageMetadata {
            product: "mskdsp-platform".into(),
            platform: "linux-amd64".into(),
            version: "0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa".into(),
            file_name: "platform.binary.tar.gz".into(),
            size: data.len() as u64,
            sha256: format!("{:x}", Sha256::digest(data)),
            source_ref: "main".into(),
            source_sha: "a".repeat(40),
            build_time: "2026-10-18T09:42:36Z".into(),
            channel: "ci".into(),
            ..Default::default()
        },
        data,
    )
    .await;
    let (mut client, _running) = client(store.clone()).await;
    for id in [&upper.id, &platform.id] {
        assert_eq!(
            client
                .download_package(proto::DownloadPackageRequest {
                    package_id: id.clone(),
                    offset: 0,
                })
                .await
                .unwrap_err()
                .code(),
            Code::NotFound
        );
    }
    store.publish(&upper.id, "ci").await.unwrap();
    store.withdraw(&upper.id, "ci").await.unwrap();
    assert_eq!(collect(&mut client, &upper, 0).await, data);
}

// 真正写坏磁盘文件，截短和同长度篡改均不得发送成功 EOF。
#[tokio::test]
async fn grpc_download_rejects_truncated_and_same_length_corrupted_packages() {
    for truncated in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
        let data = b"original-file";
        let package = upload(&store, metadata("mskdsp-upper", data), data).await;
        store.publish(&package.id, "ci").await.unwrap();
        let m = &package.metadata;
        let path = directory
            .path()
            .join("packages")
            .join(&m.product)
            .join(&m.version)
            .join(&m.platform)
            .join(&m.file_name);
        std::fs::write(
            path,
            if truncated {
                &b"short"[..]
            } else {
                &b"modified-file"[..]
            },
        )
        .unwrap();
        let (mut client, _running) = client(store).await;
        assert_eq!(
            client
                .download_package(proto::DownloadPackageRequest {
                    package_id: package.id,
                    offset: 0,
                })
                .await
                .unwrap_err()
                .code(),
            Code::DataLoss
        );
    }
}
