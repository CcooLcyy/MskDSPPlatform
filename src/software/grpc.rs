pub mod proto {
    tonic::include_proto!("mskdsp.platform.software.v1");
}

use super::{models, store::SoftwareStore};
use proto::software_catalog_service_server::SoftwareCatalogService;
use proto::software_publish_service_server::SoftwarePublishService;
use sha2::{Digest, Sha256};
use std::time::Duration;
use subtle::ConstantTimeEq;
use tonic::{Request, Response, Status, Streaming};

pub use proto::software_catalog_service_server::SoftwareCatalogServiceServer;
pub use proto::software_publish_service_server::SoftwarePublishServiceServer;

const MAX_CHUNK_BYTES: usize = 1024 * 1024;
const STREAM_IDLE_TIMEOUT: Duration = Duration::from_secs(60);
const STREAM_TOTAL_TIMEOUT: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum UploadPermission {
    Publisher,
    PlatformCi,
}

#[derive(Clone)]
pub struct PublishGrpcService {
    store: SoftwareStore,
    token_sha256: String,
    ci_upload_token_sha256: String,
    public_base_url: String,
}

impl PublishGrpcService {
    pub fn new(store: SoftwareStore, token_sha256: String, public_base_url: String) -> Self {
        Self {
            store,
            token_sha256,
            ci_upload_token_sha256: String::new(),
            public_base_url,
        }
    }

    pub fn with_ci_upload_token(mut self, token_sha256: String) -> Self {
        self.ci_upload_token_sha256 = token_sha256;
        self
    }

    fn authenticate<T>(&self, request: &Request<T>) -> Result<UploadPermission, Box<Status>> {
        let mut values = request.metadata().get_all("authorization").iter();
        let value = values.next().and_then(|value| value.to_str().ok());
        if values.next().is_some() {
            return Err(Box::new(Status::unauthenticated("软件发布凭据无效")));
        }
        let token = value
            .and_then(|value| value.strip_prefix("Bearer "))
            .filter(|token| {
                !token.is_empty() && !token.bytes().any(|byte| byte.is_ascii_whitespace())
            })
            .ok_or_else(|| Box::new(Status::unauthenticated("软件发布凭据无效")))?;
        let digest = format!("{:x}", Sha256::digest(token.as_bytes()));
        // 摘要恒时比较；配置碰撞也优先采用 CI 限权，避免意外赋予发布权限。
        let matches = |expected: &str| {
            expected.len() == 64
                && expected.bytes().all(|b| b.is_ascii_hexdigit())
                && bool::from(
                    digest
                        .as_bytes()
                        .ct_eq(expected.to_ascii_lowercase().as_bytes()),
                )
        };
        if matches(&self.ci_upload_token_sha256) {
            return Ok(UploadPermission::PlatformCi);
        }
        if matches(&self.token_sha256) {
            return Ok(UploadPermission::Publisher);
        }
        Err(Box::new(Status::unauthenticated("软件发布凭据无效")))
    }

    fn authorize_metadata(
        permission: UploadPermission,
        metadata: &models::PackageMetadata,
    ) -> Result<(), Status> {
        if permission == UploadPermission::PlatformCi {
            super::store::platform_ci_identity(metadata).map_err(|_| {
                Status::permission_denied("CI 上传令牌仅允许操作平台自身的 main CI 包")
            })?;
        }
        Ok(())
    }

    fn require_publisher(permission: UploadPermission) -> Result<(), Status> {
        if permission != UploadPermission::Publisher {
            return Err(Status::permission_denied(
                "CI 上传令牌不允许发布或撤回软件版本",
            ));
        }
        Ok(())
    }

    async fn authorize_upload(&self, permission: UploadPermission, id: &str) -> Result<(), Status> {
        if permission == UploadPermission::PlatformCi {
            let metadata = self.store.upload_metadata(id).await.map_err(store_status)?;
            Self::authorize_metadata(permission, &metadata)?;
        }
        Ok(())
    }

    fn require_public_base_url(&self) -> Result<(), Box<Status>> {
        let url = url::Url::parse(&self.public_base_url)
            .map_err(|_| Box::new(Status::failed_precondition("请配置有效的软件仓库公开地址")))?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(Box::new(Status::failed_precondition(
                "请配置有效的软件仓库公开地址",
            )));
        }
        Ok(())
    }

    async fn receive_chunks(
        &self,
        mut inbound: Streaming<proto::UploadPackageRequest>,
        permission: UploadPermission,
    ) -> Result<models::Upload, Status> {
        let mut upload_id = None;
        let mut progress = None;
        loop {
            let message = tokio::time::timeout(STREAM_IDLE_TIMEOUT, inbound.message())
                .await
                .map_err(|_| Status::deadline_exceeded("上传流空闲超时，请查询进度后续传"))??;
            let Some(message) = message else { break };
            if message.upload_id.is_empty() || message.data.is_empty() {
                return Err(Status::invalid_argument(
                    "每个分块必须包含上传 ID 和非空数据",
                ));
            }
            if message.data.len() > MAX_CHUNK_BYTES {
                return Err(Status::resource_exhausted("单个上传分块不能超过 1 MiB"));
            }
            if let Some(expected) = &upload_id {
                if expected != &message.upload_id {
                    return Err(Status::invalid_argument("同一上传流不能混用上传 ID"));
                }
            } else {
                upload_id = Some(message.upload_id.clone());
            }
            self.authorize_upload(permission, &message.upload_id)
                .await?;
            progress = Some(
                self.store
                    .append_chunk(&message.upload_id, message.offset, message.data)
                    .await
                    .map_err(store_status)?,
            );
        }
        progress.ok_or_else(|| Status::invalid_argument("上传流至少需要一个非空分块"))
    }
}

#[tonic::async_trait]
impl SoftwarePublishService for PublishGrpcService {
    async fn create_upload(
        &self,
        request: Request<proto::CreateUploadRequest>,
    ) -> Result<Response<proto::CreateUploadResponse>, Status> {
        let permission = self.authenticate(&request).map_err(|failure| *failure)?;
        let metadata = request
            .into_inner()
            .metadata
            .ok_or_else(|| Status::invalid_argument("缺少安装包元数据"))?;
        let metadata = metadata.into();
        Self::authorize_metadata(permission, &metadata)?;
        let upload = self
            .store
            .create_upload(metadata)
            .await
            .map_err(store_status)?;
        Ok(Response::new(proto::CreateUploadResponse {
            upload: Some(upload.into()),
        }))
    }

    async fn upload_package(
        &self,
        request: Request<Streaming<proto::UploadPackageRequest>>,
    ) -> Result<Response<proto::UploadPackageResponse>, Status> {
        let permission = self.authenticate(&request).map_err(|failure| *failure)?;
        let upload = tokio::time::timeout(
            STREAM_TOTAL_TIMEOUT,
            self.receive_chunks(request.into_inner(), permission),
        )
        .await
        .map_err(|_| Status::deadline_exceeded("上传流总时长超限，请查询进度后续传"))??;
        Ok(Response::new(proto::UploadPackageResponse {
            upload: Some(upload.into()),
        }))
    }

    async fn get_upload(
        &self,
        request: Request<proto::GetUploadRequest>,
    ) -> Result<Response<proto::GetUploadResponse>, Status> {
        let permission = self.authenticate(&request).map_err(|failure| *failure)?;
        let upload_id = request.into_inner().upload_id;
        self.authorize_upload(permission, &upload_id).await?;
        let upload = self
            .store
            .get_upload(&upload_id)
            .await
            .map_err(store_status)?;
        Ok(Response::new(proto::GetUploadResponse {
            upload: Some(upload.into()),
        }))
    }

    async fn complete_upload(
        &self,
        request: Request<proto::CompleteUploadRequest>,
    ) -> Result<Response<proto::CompleteUploadResponse>, Status> {
        let permission = self.authenticate(&request).map_err(|failure| *failure)?;
        let upload_id = request.into_inner().upload_id;
        self.authorize_upload(permission, &upload_id).await?;
        let package = self
            .store
            .complete_upload(&upload_id)
            .await
            .map_err(store_status)?;
        Ok(Response::new(proto::CompleteUploadResponse {
            package: Some(package.into()),
        }))
    }

    async fn publish_release(
        &self,
        request: Request<proto::PublishReleaseRequest>,
    ) -> Result<Response<proto::PublishReleaseResponse>, Status> {
        Self::require_publisher(self.authenticate(&request).map_err(|failure| *failure)?)?;
        self.require_public_base_url().map_err(|failure| *failure)?;
        let message = request.into_inner();
        let package = self
            .store
            .publish(&message.package_id, &message.channel)
            .await
            .map_err(store_status)?;
        Ok(Response::new(proto::PublishReleaseResponse {
            package: Some(package.into()),
        }))
    }

    async fn withdraw_release(
        &self,
        request: Request<proto::WithdrawReleaseRequest>,
    ) -> Result<Response<proto::WithdrawReleaseResponse>, Status> {
        Self::require_publisher(self.authenticate(&request).map_err(|failure| *failure)?)?;
        let message = request.into_inner();
        let package = self
            .store
            .withdraw(&message.package_id, &message.channel)
            .await
            .map_err(store_status)?;
        Ok(Response::new(proto::WithdrawReleaseResponse {
            package: Some(package.into()),
        }))
    }
}

#[derive(Clone)]
pub struct CatalogGrpcService {
    store: SoftwareStore,
}

impl CatalogGrpcService {
    pub fn new(store: SoftwareStore) -> Self {
        Self { store }
    }
}

#[tonic::async_trait]
impl SoftwareCatalogService for CatalogGrpcService {
    async fn list_packages(
        &self,
        request: Request<proto::ListPackagesRequest>,
    ) -> Result<Response<proto::ListPackagesResponse>, Status> {
        let filter = request.into_inner();
        let packages = self
            .store
            .list_packages()
            .await
            .map_err(store_status)?
            .into_iter()
            .filter(|package| {
                package.ever_published
                    && package.metadata.product != "mskdsp-platform"
                    && (filter.product.is_empty() || package.metadata.product == filter.product)
                    && (filter.platform.is_empty() || package.metadata.platform == filter.platform)
                    && (filter.channel.is_empty() || package.channels.contains(&filter.channel))
            })
            .map(Into::into)
            .collect();
        Ok(Response::new(proto::ListPackagesResponse { packages }))
    }

    async fn get_package(
        &self,
        request: Request<proto::GetPackageRequest>,
    ) -> Result<Response<proto::GetPackageResponse>, Status> {
        let package = self
            .store
            .get_package(&request.into_inner().package_id)
            .await
            .map_err(store_status)?;
        if !package.ever_published || package.metadata.product == "mskdsp-platform" {
            return Err(Status::not_found("安装包不存在"));
        }
        Ok(Response::new(proto::GetPackageResponse {
            package: Some(package.into()),
        }))
    }
}

fn store_status(error: models::StoreError) -> Status {
    match error {
        models::StoreError::Invalid(message) => Status::invalid_argument(message),
        models::StoreError::NotFound(_) => Status::not_found("安装包或上传任务不存在"),
        models::StoreError::Conflict(message) => Status::failed_precondition(message),
        models::StoreError::Limit(message) => Status::resource_exhausted(message),
        models::StoreError::Storage(_) => Status::internal("软件仓库存储操作失败"),
    }
}

impl From<proto::PackageMetadata> for models::PackageMetadata {
    fn from(value: proto::PackageMetadata) -> Self {
        Self {
            product: value.product,
            version: value.version,
            platform: value.platform,
            file_name: value.file_name,
            size: value.size,
            sha256: value.sha256,
            signature: value.signature,
            notes: value.notes,
            package_version: value.package_version,
            image_id: value.image_id,
            source_repository: value.source_repository,
            source_ref: value.source_ref,
            source_sha: value.source_sha,
            build_time: value.build_time,
            channel: value.channel,
        }
    }
}

impl From<models::PackageMetadata> for proto::PackageMetadata {
    fn from(value: models::PackageMetadata) -> Self {
        Self {
            product: value.product,
            version: value.version,
            platform: value.platform,
            file_name: value.file_name,
            size: value.size,
            sha256: value.sha256,
            signature: value.signature,
            notes: value.notes,
            package_version: value.package_version,
            image_id: value.image_id,
            source_repository: value.source_repository,
            source_ref: value.source_ref,
            source_sha: value.source_sha,
            build_time: value.build_time,
            channel: value.channel,
        }
    }
}

impl From<models::Upload> for proto::Upload {
    fn from(value: models::Upload) -> Self {
        Self {
            id: value.id,
            metadata: Some(value.metadata.into()),
            received_bytes: value.received_bytes,
            state: value.state,
            package_id: value.package_id.unwrap_or_default(),
            created_at: value.created_at,
            updated_at: value.updated_at,
        }
    }
}

impl From<models::Package> for proto::Package {
    fn from(value: models::Package) -> Self {
        Self {
            id: value.id,
            metadata: Some(value.metadata.into()),
            created_at: value.created_at,
            published_at: value.published_at.unwrap_or_default(),
            channels: value.channels,
            ever_published: value.ever_published,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proto::software_catalog_service_client::SoftwareCatalogServiceClient;
    use proto::software_publish_service_client::SoftwarePublishServiceClient;
    use tokio_stream::wrappers::TcpListenerStream;

    fn auth<T>(message: T) -> Request<T> {
        let mut request = Request::new(message);
        request.metadata_mut().insert(
            "authorization",
            "Bearer test-publisher-token".parse().unwrap(),
        );
        request
    }

    #[tokio::test]
    async fn real_grpc_upload_publish_and_catalog() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().to_owned(), 4096).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let token_hash = format!("{:x}", Sha256::digest(b"test-publisher-token"));
        let (shutdown, stop) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(
            tonic::transport::Server::builder()
                .add_service(SoftwarePublishServiceServer::new(PublishGrpcService::new(
                    store.clone(),
                    token_hash,
                    "https://updates.example.com".into(),
                )))
                .add_service(SoftwareCatalogServiceServer::new(CatalogGrpcService::new(
                    store,
                )))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = stop.await;
                }),
        );
        let mut publisher = SoftwarePublishServiceClient::connect(format!("http://{address}"))
            .await
            .unwrap();
        let mut catalog = SoftwareCatalogServiceClient::connect(format!("http://{address}"))
            .await
            .unwrap();
        let bytes = b"signed installer";
        let metadata = proto::PackageMetadata {
            product: "mskdsp-upper".into(),
            version: "1.2.3".into(),
            platform: "windows-x86_64-nsis".into(),
            file_name: "installer.exe".into(),
            size: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
            signature: "test-signature".into(),
            ..Default::default()
        };
        assert_eq!(
            publisher
                .create_upload(proto::CreateUploadRequest {
                    metadata: Some(metadata.clone())
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            publisher
                .get_upload(proto::GetUploadRequest {
                    upload_id: "missing".into()
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            publisher
                .complete_upload(proto::CompleteUploadRequest {
                    upload_id: "missing".into()
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            publisher
                .publish_release(proto::PublishReleaseRequest {
                    package_id: "missing".into(),
                    channel: "stable".into()
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            publisher
                .withdraw_release(proto::WithdrawReleaseRequest {
                    package_id: "missing".into(),
                    channel: "stable".into()
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            publisher
                .upload_package(tokio_stream::iter([proto::UploadPackageRequest::default()]))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
        let upload = publisher
            .create_upload(auth(proto::CreateUploadRequest {
                metadata: Some(metadata),
            }))
            .await
            .unwrap()
            .into_inner()
            .upload
            .unwrap();
        assert_eq!(
            publisher
                .upload_package(auth(tokio_stream::iter(
                    Vec::<proto::UploadPackageRequest>::new()
                )))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::InvalidArgument
        );
        let wrong_offset = proto::UploadPackageRequest {
            upload_id: upload.id.clone(),
            offset: 1,
            data: vec![1],
        };
        assert_eq!(
            publisher
                .upload_package(auth(tokio_stream::iter([wrong_offset])))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::FailedPrecondition
        );
        let too_large = proto::UploadPackageRequest {
            upload_id: upload.id.clone(),
            offset: 0,
            data: vec![1; MAX_CHUNK_BYTES + 1],
        };
        assert_eq!(
            publisher
                .upload_package(auth(tokio_stream::iter([too_large])))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::ResourceExhausted
        );
        let first = proto::UploadPackageRequest {
            upload_id: upload.id.clone(),
            offset: 0,
            data: bytes[..5].to_vec(),
        };
        publisher
            .upload_package(auth(tokio_stream::iter([first])))
            .await
            .unwrap();
        assert_eq!(
            publisher
                .get_upload(auth(proto::GetUploadRequest {
                    upload_id: upload.id.clone()
                }))
                .await
                .unwrap()
                .into_inner()
                .upload
                .unwrap()
                .received_bytes,
            5
        );
        let second = proto::UploadPackageRequest {
            upload_id: upload.id.clone(),
            offset: 5,
            data: bytes[5..].to_vec(),
        };
        publisher
            .upload_package(auth(tokio_stream::iter([second])))
            .await
            .unwrap();
        let package = publisher
            .complete_upload(auth(proto::CompleteUploadRequest {
                upload_id: upload.id.clone(),
            }))
            .await
            .unwrap()
            .into_inner()
            .package
            .unwrap();
        let repeated = publisher
            .complete_upload(auth(proto::CompleteUploadRequest {
                upload_id: upload.id,
            }))
            .await
            .unwrap()
            .into_inner()
            .package
            .unwrap();
        assert_eq!(package.id, repeated.id);
        assert_eq!(
            catalog
                .get_package(proto::GetPackageRequest {
                    package_id: package.id.clone()
                })
                .await
                .unwrap_err()
                .code(),
            tonic::Code::NotFound
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
        publisher
            .publish_release(auth(proto::PublishReleaseRequest {
                package_id: package.id.clone(),
                channel: "ci".into(),
            }))
            .await
            .unwrap();
        let published = catalog
            .get_package(proto::GetPackageRequest {
                package_id: package.id.clone(),
            })
            .await
            .unwrap()
            .into_inner()
            .package
            .unwrap();
        assert_eq!(published.channels, vec!["ci"]);
        assert_eq!(
            catalog
                .list_packages(proto::ListPackagesRequest::default())
                .await
                .unwrap()
                .into_inner()
                .packages
                .len(),
            1
        );
        publisher
            .withdraw_release(auth(proto::WithdrawReleaseRequest {
                package_id: package.id.clone(),
                channel: "ci".into(),
            }))
            .await
            .unwrap();
        assert!(
            catalog
                .get_package(proto::GetPackageRequest {
                    package_id: package.id
                })
                .await
                .unwrap()
                .into_inner()
                .package
                .unwrap()
                .channels
                .is_empty()
        );
        let _ = shutdown.send(());
        server.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn publisher_disabled_by_default_and_invalid_base_url_rejected() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().to_owned(), 4096).unwrap();
        let disabled = PublishGrpcService::new(store.clone(), String::new(), String::new());
        assert_eq!(
            disabled.authenticate(&auth(())).unwrap_err().code(),
            tonic::Code::Unauthenticated
        );
        let enabled = PublishGrpcService::new(
            store,
            format!("{:x}", Sha256::digest(b"test-publisher-token")),
            String::new(),
        );
        assert_eq!(
            enabled.require_public_base_url().unwrap_err().code(),
            tonic::Code::FailedPrecondition
        );
        let mut duplicate = auth(());
        duplicate.metadata_mut().append(
            "authorization",
            "Bearer test-publisher-token".parse().unwrap(),
        );
        assert_eq!(
            enabled.authenticate(&duplicate).unwrap_err().code(),
            tonic::Code::Unauthenticated
        );
    }
}
