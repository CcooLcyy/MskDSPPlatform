use super::{
    grpc::{
        CatalogGrpcService, PublishGrpcService, SoftwareCatalogServiceServer,
        SoftwarePublishServiceServer, proto,
    },
    store::SoftwareStore,
};
use crate::{
    auth::{AdminAuth, AuthConfig},
    http::HttpState,
    state::SessionRegistry,
};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, transport::Server};

const TOKEN: &str = "local-integration-token-with-at-least-32-bytes";
const CI_TOKEN: &str = "local-ci-upload-token-with-at-least-32-bytes";
const TESTBENCH_TOKEN: &str = "local-testbench-token-with-at-least-32-bytes";

fn testbench_authenticated<T>(message: T) -> Request<T> {
    let mut request = Request::new(message);
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {TESTBENCH_TOKEN}").parse().unwrap(),
    );
    request
}

fn ci_authenticated<T>(message: T) -> Request<T> {
    let mut request = Request::new(message);
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {CI_TOKEN}").parse().unwrap(),
    );
    request
}

// 真实 gRPC 验证 CI 令牌仅能上传平台包：混流、跨产品查询/完成和公开发布均拒绝。
#[tokio::test]
async fn ci_upload_token_is_scoped_to_private_platform_candidates() {
    let directory = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(directory.path().into(), 4096).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let publisher = PublishGrpcService::new(
        store.clone(),
        format!("{:x}", Sha256::digest(TOKEN)),
        String::new(),
    )
    .with_ci_upload_token(format!("{:x}", Sha256::digest(CI_TOKEN)));
    let _running = RunningServices {
        http: tokio::spawn(async {}),
        grpc: tokio::spawn(async move {
            Server::builder()
                .add_service(SoftwarePublishServiceServer::new(publisher))
                .add_service(SoftwareCatalogServiceServer::new(CatalogGrpcService::new(
                    store,
                )))
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await
                .unwrap();
        }),
    };
    let mut client = proto::software_publish_service_client::SoftwarePublishServiceClient::connect(
        format!("http://{address}"),
    )
    .await
    .unwrap();
    let mut catalog =
        proto::software_catalog_service_client::SoftwareCatalogServiceClient::connect(format!(
            "http://{address}"
        ))
        .await
        .unwrap();
    let bytes = b"platform-candidate";
    let platform = proto::PackageMetadata {
        product: "mskdsp-platform".into(),
        platform: "linux-amd64".into(),
        version: "0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa".into(),
        file_name: "platform.binary.tar.gz".into(),
        size: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
        source_ref: "main".into(),
        source_sha: "a".repeat(40),
        build_time: "2026-10-18T09:42:36Z".into(),
        channel: "ci".into(),
        ..Default::default()
    };
    let lower = proto::PackageMetadata {
        product: "mskdsp-lower".into(),
        platform: "linux-arm64".into(),
        version: "1.0.0".into(),
        file_name: "lower.tar.gz".into(),
        size: bytes.len() as u64,
        sha256: platform.sha256.clone(),
        package_version: "1.0.0".into(),
        image_id: format!("sha256:{}", "b".repeat(64)),
        ..Default::default()
    };
    assert_eq!(
        client
            .create_upload(ci_authenticated(proto::CreateUploadRequest {
                metadata: Some(lower.clone())
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    let lower_upload = client
        .create_upload(authenticated(proto::CreateUploadRequest {
            metadata: Some(lower),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    assert_eq!(
        client
            .get_upload(ci_authenticated(proto::GetUploadRequest {
                upload_id: lower_upload.id.clone()
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    assert_eq!(
        client
            .complete_upload(ci_authenticated(proto::CompleteUploadRequest {
                upload_id: lower_upload.id.clone()
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    assert_eq!(
        client
            .upload_package(ci_authenticated(tokio_stream::iter([
                proto::UploadPackageRequest {
                    upload_id: lower_upload.id.clone(),
                    offset: 0,
                    data: bytes.to_vec()
                }
            ])))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    let task = client
        .create_upload(ci_authenticated(proto::CreateUploadRequest {
            metadata: Some(platform.clone()),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    // 首个合法平台分块不能为后续其他产品的分块获得授权。
    assert!(
        client
            .upload_package(ci_authenticated(tokio_stream::iter([
                proto::UploadPackageRequest {
                    upload_id: task.id.clone(),
                    offset: 0,
                    data: bytes[..4].to_vec()
                },
                proto::UploadPackageRequest {
                    upload_id: lower_upload.id.clone(),
                    offset: 0,
                    data: bytes.to_vec()
                },
            ])))
            .await
            .is_err()
    );
    let progress = client
        .get_upload(ci_authenticated(proto::GetUploadRequest {
            upload_id: task.id.clone(),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    assert_eq!(progress.received_bytes, 4);
    let lower_progress = client
        .get_upload(authenticated(proto::GetUploadRequest {
            upload_id: lower_upload.id.clone(),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    assert_eq!(lower_progress.received_bytes, 0);
    client
        .upload_package(ci_authenticated(tokio_stream::iter([
            proto::UploadPackageRequest {
                upload_id: task.id.clone(),
                offset: 4,
                data: bytes[4..].to_vec(),
            },
        ])))
        .await
        .unwrap();
    let package = client
        .complete_upload(ci_authenticated(proto::CompleteUploadRequest {
            upload_id: task.id.clone(),
        }))
        .await
        .unwrap()
        .into_inner()
        .package
        .unwrap();
    assert_eq!(package.metadata, Some(platform));
    assert!(!package.ever_published && package.channels.is_empty());
    assert_eq!(
        client
            .complete_upload(ci_authenticated(proto::CompleteUploadRequest {
                upload_id: task.id
            }))
            .await
            .unwrap()
            .into_inner()
            .package
            .unwrap()
            .id,
        package.id
    );
    assert_eq!(
        client
            .publish_release(ci_authenticated(proto::PublishReleaseRequest {
                package_id: package.id.clone(),
                channel: "ci".into()
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    assert_eq!(
        client
            .withdraw_release(ci_authenticated(proto::WithdrawReleaseRequest {
                package_id: package.id.clone(),
                channel: "ci".into()
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::PermissionDenied
    );
    assert_eq!(
        client
            .withdraw_release(authenticated(proto::WithdrawReleaseRequest {
                package_id: package.id.clone(),
                channel: "ci".into()
            }))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::InvalidArgument
    );
    assert!(
        catalog
            .list_packages(proto::ListPackagesRequest::default())
            .await
            .unwrap()
            .into_inner()
            .packages
            .is_empty()
    );
    assert_eq!(
        catalog
            .get_package(proto::GetPackageRequest {
                package_id: package.id
            })
            .await
            .unwrap_err()
            .code(),
        tonic::Code::NotFound
    );
}

fn authenticated<T>(message: T) -> Request<T> {
    let mut request = Request::new(message);
    request
        .metadata_mut()
        .insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
    request
}

struct RunningServices {
    grpc: tokio::task::JoinHandle<()>,
    http: tokio::task::JoinHandle<()>,
}

impl Drop for RunningServices {
    fn drop(&mut self) {
        self.grpc.abort();
        self.http.abort();
    }
}

async fn http_request(
    address: std::net::SocketAddr,
    method: &str,
    path: &str,
    range: Option<&str>,
) -> (u16, String, Vec<u8>) {
    let mut connection = TcpStream::connect(address).await.unwrap();
    let range = range
        .map(|value| format!("Range: {value}\r\n"))
        .unwrap_or_default();
    connection
        .write_all(
            format!(
                "{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n{range}\r\n"
            )
            .as_bytes(),
        )
        .await
        .unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(
        std::time::Duration::from_secs(5),
        connection.read_to_end(&mut response),
    )
    .await
    .unwrap()
    .unwrap();
    let boundary = response
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .unwrap();
    let headers = String::from_utf8(response[..boundary].to_vec()).unwrap();
    let status = headers.split_whitespace().nth(1).unwrap().parse().unwrap();
    (status, headers, response[boundary + 4..].to_vec())
}

// 通过真实随机端口验证 gRPC 上传、发布、公开目录和 HTTP 原始字节下载的完整闭环。
#[tokio::test]
async fn grpc_upload_and_http_download_preserve_package_and_manifest_contracts() {
    let temporary = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(temporary.path().join("software"), 1024 * 1024).unwrap();
    let http_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let http_address = http_listener.local_addr().unwrap();
    let grpc_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let grpc_address = grpc_listener.local_addr().unwrap();
    let public_base_url = format!("http://{http_address}");
    let state = HttpState {
        sessions: SessionRegistry::default(),
        protocol_version: Arc::from("platform.v1"),
        auth: AdminAuth::new(AuthConfig::default()).unwrap(),
    };
    let router = super::http::router(state, store.clone(), public_base_url.clone());
    let publisher = PublishGrpcService::new(
        store.clone(),
        format!("{:x}", Sha256::digest(TOKEN.as_bytes())),
        public_base_url.clone(),
    )
    .with_testbench_publisher_token(format!("{:x}", Sha256::digest(TESTBENCH_TOKEN)));
    let catalog = CatalogGrpcService::new(store.clone());
    let _running = RunningServices {
        http: tokio::spawn(async move {
            axum::serve(http_listener, router).await.unwrap();
        }),
        grpc: tokio::spawn(async move {
            Server::builder()
                .add_service(SoftwarePublishServiceServer::new(publisher))
                .add_service(SoftwareCatalogServiceServer::new(catalog))
                .serve_with_incoming(TcpListenerStream::new(grpc_listener))
                .await
                .unwrap();
        }),
    };
    let mut client = proto::software_publish_service_client::SoftwarePublishServiceClient::connect(
        format!("http://{grpc_address}"),
    )
    .await
    .unwrap();
    let mut catalog_client =
        proto::software_catalog_service_client::SoftwareCatalogServiceClient::connect(format!(
            "http://{grpc_address}"
        ))
        .await
        .unwrap();
    let bytes = b"local-package-bytes\0\x01\xff";
    let digest = format!("{:x}", Sha256::digest(bytes));
    let metadata = proto::PackageMetadata {
        product: "mskdsp-lower".into(),
        version: "1.2.3".into(),
        platform: "linux-arm64".into(),
        file_name: "mskdsp-1.2.3.run".into(),
        size: bytes.len() as u64,
        sha256: digest.clone(),
        package_version: "1.2.3".into(),
        image_id: format!("sha256:{}", "a".repeat(64)),
        source_repository: "mskdsp/mskdsp".into(),
        source_ref: "refs/tags/v1.2.3".into(),
        source_sha: "b".repeat(40),
        ..Default::default()
    };
    let error = client
        .create_upload(proto::CreateUploadRequest {
            metadata: Some(metadata.clone()),
        })
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::Unauthenticated);
    let upload = client
        .create_upload(authenticated(proto::CreateUploadRequest {
            metadata: Some(metadata),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    for (offset, data) in [(0, bytes[..7].to_vec()), (7, bytes[7..].to_vec())] {
        client
            .upload_package(authenticated(tokio_stream::iter(vec![
                proto::UploadPackageRequest {
                    upload_id: upload.id.clone(),
                    offset,
                    data,
                },
            ])))
            .await
            .unwrap();
    }
    let progress = client
        .get_upload(authenticated(proto::GetUploadRequest {
            upload_id: upload.id.clone(),
        }))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    assert_eq!(progress.received_bytes, bytes.len() as u64);
    let package = client
        .complete_upload(authenticated(proto::CompleteUploadRequest {
            upload_id: upload.id,
        }))
        .await
        .unwrap()
        .into_inner()
        .package
        .unwrap();
    assert_eq!(
        http_request(
            http_address,
            "GET",
            "/mskdsp-lower/stable/latest.json",
            None
        )
        .await
        .0,
        404
    );
    let error = catalog_client
        .get_package(proto::GetPackageRequest {
            package_id: package.id.clone(),
        })
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::NotFound);
    client
        .publish_release(authenticated(proto::PublishReleaseRequest {
            package_id: package.id.clone(),
            channel: "stable".into(),
        }))
        .await
        .unwrap();
    let (status, headers, body) = http_request(
        http_address,
        "GET",
        "/mskdsp-lower/stable/latest.json",
        None,
    )
    .await;
    assert_eq!(status, 200);
    assert!(
        headers
            .to_ascii_lowercase()
            .contains("cache-control: no-store")
    );
    let manifest: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(manifest["schema_version"], 1);
    assert_eq!(manifest["asset"]["sha256"], digest);
    assert_eq!(manifest["image_id"], format!("sha256:{}", "a".repeat(64)));
    let url = url::Url::parse(manifest["asset"]["url"].as_str().unwrap()).unwrap();
    let (status, _, downloaded) = http_request(http_address, "GET", url.path(), None).await;
    assert_eq!(status, 200);
    assert_eq!(downloaded, bytes);
    let (status, headers, partial) =
        http_request(http_address, "GET", url.path(), Some("bytes=3-8")).await;
    assert_eq!(status, 206);
    assert!(
        headers
            .to_ascii_lowercase()
            .contains("content-range: bytes 3-8/")
    );
    assert_eq!(partial, bytes[3..=8]);
    let (status, _, body) = http_request(http_address, "HEAD", url.path(), None).await;
    assert_eq!(status, 200);
    assert!(body.is_empty());
    let checksum_url = url::Url::parse(manifest["checksum"]["url"].as_str().unwrap()).unwrap();
    let checksum = http_request(http_address, "GET", checksum_url.path(), None).await;
    assert_eq!(checksum.0, 200);
    assert!(String::from_utf8(checksum.2).unwrap().contains(&digest));
    client
        .withdraw_release(authenticated(proto::WithdrawReleaseRequest {
            package_id: package.id.clone(),
            channel: "stable".into(),
        }))
        .await
        .unwrap();
    assert_eq!(
        http_request(
            http_address,
            "GET",
            "/mskdsp-lower/stable/latest.json",
            None
        )
        .await
        .0,
        404
    );
    assert_eq!(
        http_request(http_address, "GET", url.path(), None).await.0,
        200
    );
    let restored = SoftwareStore::new(temporary.path().join("software"), 1024 * 1024).unwrap();
    assert!(
        restored
            .get_package(&package.id)
            .await
            .unwrap()
            .ever_published
    );

    let manifest_path = "/mskdsp-testbench/stable/latest.json";
    let metadata = proto::PackageMetadata {
        product: super::TESTBENCH_PRODUCT.into(),
        version: "2.0.0".into(),
        platform: "windows-x86_64-nsis".into(),
        file_name: "mskdsp-testbench_2.0.0_x64-setup.exe".into(),
        size: bytes.len() as u64,
        sha256: digest,
        signature: "testbench-updater-signature".into(),
        notes: "Testbench release notes".into(),
        channel: "stable".into(),
        ..Default::default()
    };
    let create = proto::CreateUploadRequest {
        metadata: Some(metadata.clone()),
    };
    let error = client
        .create_upload(authenticated(create.clone()))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    for invalid_metadata in [
        proto::PackageMetadata {
            signature: String::new(),
            ..metadata.clone()
        },
        proto::PackageMetadata {
            platform: "windows-x86_64".into(),
            ..metadata.clone()
        },
    ] {
        let error = client
            .create_upload(testbench_authenticated(proto::CreateUploadRequest {
                metadata: Some(invalid_metadata),
            }))
            .await
            .unwrap_err();
        assert_eq!(error.code(), tonic::Code::InvalidArgument);
    }
    let upload = client
        .create_upload(testbench_authenticated(create))
        .await
        .unwrap()
        .into_inner()
        .upload
        .unwrap();
    let stream_chunk = proto::UploadPackageRequest {
        upload_id: upload.id.clone(),
        offset: 0,
        data: bytes.to_vec(),
    };
    let error = client
        .upload_package(authenticated(tokio_stream::iter([stream_chunk.clone()])))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    client
        .upload_package(testbench_authenticated(tokio_stream::iter([stream_chunk])))
        .await
        .unwrap();
    let testbench = client
        .complete_upload(testbench_authenticated(proto::CompleteUploadRequest {
            upload_id: upload.id,
        }))
        .await
        .unwrap()
        .into_inner()
        .package
        .unwrap();
    let path = format!("/software/packages/{}/{}", testbench.id, metadata.file_name);
    assert_eq!(http_request(http_address, "GET", &path, None).await.0, 404);
    let (status, headers, body) = http_request(http_address, "GET", manifest_path, None).await;
    assert_eq!(status, 204);
    assert!(
        headers
            .to_ascii_lowercase()
            .contains("cache-control: no-store")
    );
    assert!(body.is_empty());
    for (request, expected) in [
        (
            authenticated(proto::PublishReleaseRequest {
                package_id: testbench.id.clone(),
                channel: "stable".into(),
            }),
            tonic::Code::PermissionDenied,
        ),
        (
            testbench_authenticated(proto::PublishReleaseRequest {
                package_id: package.id,
                channel: "stable".into(),
            }),
            tonic::Code::PermissionDenied,
        ),
        (
            testbench_authenticated(proto::PublishReleaseRequest {
                package_id: testbench.id.clone(),
                channel: "ci".into(),
            }),
            tonic::Code::InvalidArgument,
        ),
    ] {
        assert_eq!(
            client.publish_release(request).await.unwrap_err().code(),
            expected
        );
    }
    // 测试上位机的 gRPC 发布和目录独立于 HTTP 下载地址。
    let publisher_without_http = PublishGrpcService::new(store, String::new(), String::new())
        .with_testbench_publisher_token(format!("{:x}", Sha256::digest(TESTBENCH_TOKEN)));
    proto::software_publish_service_server::SoftwarePublishService::publish_release(
        &publisher_without_http,
        testbench_authenticated(proto::PublishReleaseRequest {
            package_id: testbench.id.clone(),
            channel: "stable".into(),
        }),
    )
    .await
    .unwrap();
    let catalog = catalog_client
        .list_packages(proto::ListPackagesRequest {
            product: metadata.product.clone(),
            channel: "stable".into(),
            platform: metadata.platform.clone(),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(catalog.packages.len(), 1);
    assert_eq!(catalog.packages[0].id, testbench.id);
    let (status, _, body) = http_request(http_address, "GET", manifest_path, None).await;
    assert_eq!(status, 200);
    let manifest: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(manifest["version"], metadata.version);
    assert_eq!(manifest["notes"], metadata.notes);
    assert!(manifest["pub_date"].is_string());
    assert_eq!(
        manifest["platforms"],
        serde_json::json!({
            "windows-x86_64-nsis": {
                "url": format!("{public_base_url}{path}"),
                "signature": metadata.signature,
            }
        })
    );
    let (status, _, downloaded) = http_request(http_address, "GET", &path, None).await;
    assert_eq!(status, 200);
    assert_eq!(downloaded, bytes);
    let withdraw = proto::WithdrawReleaseRequest {
        package_id: testbench.id,
        channel: "stable".into(),
    };
    let error = client
        .withdraw_release(authenticated(withdraw.clone()))
        .await
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    client
        .withdraw_release(testbench_authenticated(withdraw))
        .await
        .unwrap();
    assert_eq!(
        http_request(http_address, "GET", manifest_path, None)
            .await
            .0,
        204
    );
    assert_eq!(http_request(http_address, "GET", &path, None).await.0, 200);
}
