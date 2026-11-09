use super::{models::StoreError, store::SoftwareStore};
use crate::{auth, http::HttpState};
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Path, Request, State, rejection::JsonRejection},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
#[cfg(test)]
use serde_json::Value;
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncSeekExt};
use tokio_util::io::ReaderStream;
use tracing::warn;

#[derive(Clone)]
struct SoftwareState {
    auth: HttpState,
    store: SoftwareStore,
    public_base_url: String,
}

pub fn router(auth: HttpState, store: SoftwareStore, public_base_url: String) -> Router {
    let state = SoftwareState {
        auth,
        store,
        public_base_url,
    };
    let admin = Router::new()
        .route("/api/v1/software/packages", get(list))
        .route("/api/v1/software/packages/{id}", get(detail))
        .route("/api/v1/software/packages/{id}/publish", post(publish))
        .route("/api/v1/software/packages/{id}/withdraw", post(withdraw))
        .layer(DefaultBodyLimit::max(4096))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_admin))
        .layer(middleware::from_fn(auth::no_cache));
    let page = Router::new()
        .route(
            "/software",
            get(|| async { Html(include_str!("../../web/software.html")) }),
        )
        .route_layer(middleware::from_fn_with_state(
            state.auth.clone(),
            auth::require_admin,
        ))
        .layer(middleware::from_fn(auth::no_cache));
    Router::new()
        .merge(admin)
        .merge(page)
        .route("/mskdsp-upper/{channel}/latest.json", get(upper_manifest))
        .route(
            "/mskdsp-testbench/stable/latest.json",
            get(testbench_manifest),
        )
        .route("/mskdsp-lower/{channel}/latest.json", get(lower_manifest))
        .route("/mskdsp-lower-x64/ci/latest.json", get(lower_x64_manifest))
        .route("/software/packages/{id}/{name}", get(download))
        .with_state(state)
}

async fn require_admin(
    State(state): State<SoftwareState>,
    request: Request,
    next: Next,
) -> Response {
    let identity = auth::me(State(state.auth), request.headers().clone()).await;
    if identity.status() == StatusCode::OK {
        next.run(request).await
    } else {
        identity
    }
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error": message}))).into_response()
}

fn storage_error(failure: StoreError) -> Response {
    let (status, message) = match &failure {
        StoreError::Invalid(_) => (StatusCode::BAD_REQUEST, "软件版本或请求参数不正确"),
        StoreError::NotFound(_) => (StatusCode::NOT_FOUND, "软件版本、通道或文件不存在"),
        StoreError::Conflict(_) => (StatusCode::CONFLICT, "软件版本状态冲突，请刷新后重试"),
        StoreError::Limit(_) => (StatusCode::PAYLOAD_TOO_LARGE, "软件仓库请求超过限制"),
        StoreError::Storage(_) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            "软件仓库暂时不可用，请稍后重试",
        ),
    };
    warn!(status = status.as_u16(), error = %failure, "软件仓库操作失败");
    error(status, message)
}

fn action(headers: &HeaderMap) -> Result<(), Box<Response>> {
    let actions: Vec<_> = headers.get_all("X-MskDSP-Action").iter().collect();
    let hosts: Vec<_> = headers.get_all(header::HOST).iter().collect();
    let origins: Vec<_> = headers.get_all(header::ORIGIN).iter().collect();
    if actions.len() != 1 || actions[0] != "software" || hosts.len() != 1 || origins.len() > 1 {
        return Err(Box::new(error(
            StatusCode::FORBIDDEN,
            "软件操作标记或请求来源不正确",
        )));
    }
    let host = hosts[0].to_str().unwrap_or_default();
    if host.contains('@')
        || host.parse::<axum::http::uri::Authority>().is_err()
        || (!origins.is_empty()
            && !origins[0].to_str().is_ok_and(|origin| {
                origin.eq_ignore_ascii_case(&format!("http://{host}"))
                    || origin.eq_ignore_ascii_case(&format!("https://{host}"))
            }))
    {
        return Err(Box::new(error(StatusCode::FORBIDDEN, "不允许跨源软件操作")));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Channel {
    channel: String,
}

async fn list(State(state): State<SoftwareState>) -> Response {
    match state.store.list_packages().await {
        Ok(packages) => Json(packages).into_response(),
        Err(failure) => storage_error(failure),
    }
}

async fn detail(State(state): State<SoftwareState>, Path(id): Path<String>) -> Response {
    match state.store.get_package(&id).await {
        Ok(package) => Json(package).into_response(),
        Err(failure) => storage_error(failure),
    }
}

fn channel_body(value: Result<Json<Channel>, JsonRejection>) -> Result<Channel, Box<Response>> {
    value.map(|Json(value)| value).map_err(|failure| {
        Box::new(error(
            failure.status(),
            "软件操作请求必须是有效的 JSON 通道信息",
        ))
    })
}

async fn publish(
    State(state): State<SoftwareState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Channel>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return *response;
    }
    let channel = match channel_body(value) {
        Ok(value) => value.channel,
        Err(response) => return *response,
    };
    if state.public_base_url.is_empty() {
        return error(
            StatusCode::PRECONDITION_FAILED,
            "尚未配置软件下载公开地址，暂时不能发布",
        );
    }
    match state.store.publish(&id, &channel).await {
        Ok(package) => Json(package).into_response(),
        Err(failure) => storage_error(failure),
    }
}

async fn withdraw(
    State(state): State<SoftwareState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Channel>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return *response;
    }
    let channel = match channel_body(value) {
        Ok(value) => value.channel,
        Err(response) => return *response,
    };
    match state.store.withdraw(&id, &channel).await {
        Ok(package) => Json(package).into_response(),
        Err(failure) => storage_error(failure),
    }
}

async fn manifest(
    state: SoftwareState,
    product: &str,
    channel: &str,
    platform: Option<&str>,
) -> Response {
    let testbench = product == super::TESTBENCH_PRODUCT;
    let mut response = if state.public_base_url.is_empty() && !testbench {
        error(StatusCode::PRECONDITION_FAILED, "尚未配置软件下载公开地址")
    } else {
        match state
            .store
            .manifest(product, channel, platform, &state.public_base_url)
            .await
        {
            Ok(manifest) => Json(manifest).into_response(),
            Err(StoreError::NotFound(_)) if testbench => StatusCode::NO_CONTENT.into_response(),
            Err(StoreError::Invalid(_)) if testbench && state.public_base_url.is_empty() => {
                error(StatusCode::PRECONDITION_FAILED, "尚未配置软件下载公开地址")
            }
            Err(failure) => storage_error(failure),
        }
    };
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

async fn upper_manifest(
    State(state): State<SoftwareState>,
    Path(channel): Path<String>,
) -> Response {
    manifest(state, "mskdsp-upper", &channel, None).await
}

async fn testbench_manifest(State(state): State<SoftwareState>) -> Response {
    manifest(state, super::TESTBENCH_PRODUCT, "stable", None).await
}

async fn lower_manifest(
    State(state): State<SoftwareState>,
    Path(channel): Path<String>,
) -> Response {
    manifest(state, "mskdsp-lower", &channel, Some("linux-arm64")).await
}

async fn lower_x64_manifest(State(state): State<SoftwareState>) -> Response {
    manifest(state, "mskdsp-lower", "ci", Some("linux-x64")).await
}

// 返回闭区间范围，拒绝格式错误、多区间和越界请求。
fn byte_range(value: &str, size: u64) -> Option<(u64, u64)> {
    let value = value.strip_prefix("bytes=")?;
    let (start, end) = value.split_once('-')?;
    if size == 0 {
        return None;
    }
    let number = |text: &str| {
        (!text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()))
            .then(|| text.parse::<u64>().ok())
            .flatten()
    };
    if start.is_empty() {
        let suffix = number(end)?;
        return (suffix > 0).then_some((size.saturating_sub(suffix), size - 1));
    }
    let start = number(start)?;
    let end = if end.is_empty() {
        size - 1
    } else {
        number(end)?.min(size - 1)
    };
    (start < size && end >= start).then_some((start, end))
}

fn range_headers(headers: &HeaderMap, size: u64, etag: &str) -> Result<Option<(u64, u64)>, ()> {
    let ranges: Vec<_> = headers.get_all(header::RANGE).iter().collect();
    if ranges.is_empty() {
        return Ok(None);
    }
    if headers
        .get(header::IF_RANGE)
        .is_some_and(|value| value != etag)
    {
        return Ok(None);
    }
    if ranges.len() != 1 {
        return Err(());
    }
    byte_range(ranges[0].to_str().map_err(|_| ())?, size)
        .map(Some)
        .ok_or(())
}

async fn download(
    State(state): State<SoftwareState>,
    Path((id, name)): Path<(String, String)>,
    method: Method,
    headers: HeaderMap,
) -> Response {
    if name == "SHA256SUMS" {
        return match state.store.checksum(&id).await {
            Ok(checksum) => checksum_download(checksum, method, headers),
            Err(failure) => storage_error(failure),
        };
    }
    let file = match state.store.download_file(&id, &name).await {
        Ok(file) => file,
        Err(failure) => return storage_error(failure),
    };
    let etag = format!("\"{}\"", file.sha256);
    let mut response_headers = file_headers(file.size, &etag, "application/octet-stream");
    // 存储层只接受安全 ASCII 文件名，此处再次检查以防未来契约变化。
    if file
        .file_name
        .bytes()
        .any(|byte| !byte.is_ascii_alphanumeric() && !b"._-+".contains(&byte))
    {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "软件包文件信息不正确");
    }
    let Ok(disposition) =
        HeaderValue::from_str(&format!("attachment; filename=\"{}\"", file.file_name))
    else {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "软件包文件信息不正确");
    };
    response_headers.insert(header::CONTENT_DISPOSITION, disposition);
    if not_modified(&headers, &etag) {
        response_headers.remove(header::CONTENT_LENGTH);
        return (StatusCode::NOT_MODIFIED, response_headers).into_response();
    }
    let selected = match range_headers(&headers, file.size, &etag) {
        Ok(value) => value,
        Err(()) => return unsatisfiable(response_headers, file.size),
    };
    let (status, start, count) = select_range(&mut response_headers, selected, file.size);
    let mut input = match tokio::fs::File::open(&file.path).await {
        Ok(input) => input,
        Err(failure) => {
            warn!(error = %failure, "软件包文件读取失败");
            return error(StatusCode::INTERNAL_SERVER_ERROR, "软件包文件暂时不可用");
        }
    };
    if input.metadata().await.map_or(true, |metadata| {
        !metadata.is_file() || metadata.len() != file.size
    }) {
        return error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "软件包文件完整性检查失败",
        );
    }
    if method == Method::HEAD {
        return (status, response_headers).into_response();
    }
    if start > 0 && input.seek(std::io::SeekFrom::Start(start)).await.is_err() {
        return error(StatusCode::INTERNAL_SERVER_ERROR, "软件包文件暂时不可用");
    }
    (
        status,
        response_headers,
        Body::from_stream(ReaderStream::new(input.take(count))),
    )
        .into_response()
}

fn file_headers(size: u64, etag: &str, content_type: &'static str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    headers.insert(header::CONTENT_LENGTH, size.into());
    headers.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    headers.insert(
        header::ETAG,
        HeaderValue::from_str(etag).expect("validated SHA256 ETag"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("public, max-age=31536000, immutable"),
    );
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers
}

fn select_range(
    headers: &mut HeaderMap,
    range: Option<(u64, u64)>,
    size: u64,
) -> (StatusCode, u64, u64) {
    match range {
        Some((start, end)) => {
            headers.insert(
                header::CONTENT_RANGE,
                HeaderValue::from_str(&format!("bytes {start}-{end}/{size}")).unwrap(),
            );
            headers.insert(header::CONTENT_LENGTH, (end - start + 1).into());
            (StatusCode::PARTIAL_CONTENT, start, end - start + 1)
        }
        None => (StatusCode::OK, 0, size),
    }
}

fn unsatisfiable(mut headers: HeaderMap, size: u64) -> Response {
    headers.insert(
        header::CONTENT_RANGE,
        HeaderValue::from_str(&format!("bytes */{size}")).unwrap(),
    );
    headers.insert(header::CONTENT_LENGTH, HeaderValue::from_static("0"));
    (StatusCode::RANGE_NOT_SATISFIABLE, headers).into_response()
}

fn not_modified(headers: &HeaderMap, etag: &str) -> bool {
    headers
        .get(header::IF_NONE_MATCH)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value.split(',').any(|value| {
                let value = value.trim();
                value == "*" || value.strip_prefix("W/").unwrap_or(value) == etag
            })
        })
}

fn checksum_download(checksum: String, method: Method, headers: HeaderMap) -> Response {
    use sha2::{Digest, Sha256};
    let size = checksum.len() as u64;
    let etag = format!("\"{:x}\"", Sha256::digest(checksum.as_bytes()));
    let mut response_headers = file_headers(size, &etag, "text/plain; charset=utf-8");
    response_headers.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_static("attachment; filename=\"SHA256SUMS\""),
    );
    if not_modified(&headers, &etag) {
        response_headers.remove(header::CONTENT_LENGTH);
        return (StatusCode::NOT_MODIFIED, response_headers).into_response();
    }
    let range = match range_headers(&headers, size, &etag) {
        Ok(value) => value,
        Err(()) => return unsatisfiable(response_headers, size),
    };
    let (status, start, count) = select_range(&mut response_headers, range, size);
    let body = if method == Method::HEAD {
        Vec::new()
    } else {
        checksum.into_bytes()[start as usize..(start + count) as usize].to_vec()
    };
    (status, response_headers, body).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::to_bytes, http::Request};
    use sha2::{Digest, Sha256};
    use tower::ServiceExt;

    #[test]
    fn single_ranges_cover_open_suffix_and_reject_ambiguous_input() {
        assert_eq!(byte_range("bytes=2-4", 10), Some((2, 4)));
        assert_eq!(byte_range("bytes=7-", 10), Some((7, 9)));
        assert_eq!(byte_range("bytes=-3", 10), Some((7, 9)));
        assert_eq!(byte_range("bytes=0-99", 10), Some((0, 9)));
        assert_eq!(byte_range("bytes=-99", 10), Some((0, 9)));
        for value in [
            "bytes=10-",
            "bytes=3-2",
            "bytes=-0",
            "bytes=1-2,4-5",
            "items=0-1",
            "bytes=+1-2",
            "bytes=0-1-2",
        ] {
            assert_eq!(byte_range(value, 10), None, "{value}");
        }
        assert_eq!(byte_range("bytes=0-", 0), None);
    }

    #[test]
    fn mutations_require_unique_action_and_same_origin() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "platform.example".parse().unwrap());
        headers.insert("X-MskDSP-Action", "software".parse().unwrap());
        headers.insert(header::ORIGIN, "https://platform.example".parse().unwrap());
        assert!(action(&headers).is_ok());
        headers.insert(header::ORIGIN, "https://other.example".parse().unwrap());
        assert!(action(&headers).is_err());
        headers.remove(header::ORIGIN);
        assert!(action(&headers).is_ok());
        headers.append("X-MskDSP-Action", "software".parse().unwrap());
        assert!(action(&headers).is_err());
    }

    #[tokio::test]
    async fn public_download_manifest_and_admin_boundaries() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().to_path_buf(), 1024).unwrap();
        let bytes = b"0123456789";
        let metadata = super::super::models::PackageMetadata {
            product: "mskdsp-upper".into(),
            version: "1.2.3".into(),
            platform: "windows-x86_64-nsis".into(),
            file_name: "installer.exe".into(),
            size: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
            signature: "test-signature".into(),
            ..Default::default()
        };
        let upload = store.create_upload(metadata).await.unwrap();
        store
            .append_chunk(&upload.id, 0, bytes.to_vec())
            .await
            .unwrap();
        let package = store.complete_upload(&upload.id).await.unwrap();
        let auth = crate::http::HttpState {
            sessions: Default::default(),
            protocol_version: "test.v1".into(),
            auth: crate::auth::AdminAuth::new(Default::default()).unwrap(),
        };
        let app = router(auth, store.clone(), "https://updates.example".into());
        let path = format!("/software/packages/{}/installer.exe", package.id);
        let request = |method, path: &str| {
            Request::builder()
                .method(method)
                .uri(path)
                .body(Body::empty())
                .unwrap()
        };
        let before = app.clone().oneshot(request("GET", &path)).await.unwrap();
        assert_eq!(before.status(), StatusCode::NOT_FOUND);
        assert_eq!(
            app.clone()
                .oneshot(request("GET", "/api/v1/software/packages"))
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            app.clone()
                .oneshot(request("GET", "/software"))
                .await
                .unwrap()
                .status(),
            StatusCode::SEE_OTHER
        );
        store.publish(&package.id, "stable").await.unwrap();
        let manifest = app
            .clone()
            .oneshot(request("GET", "/mskdsp-upper/stable/latest.json"))
            .await
            .unwrap();
        assert_eq!(manifest.status(), StatusCode::OK);
        assert_eq!(manifest.headers()[header::CACHE_CONTROL], "no-store");
        let json: Value =
            serde_json::from_slice(&to_bytes(manifest.into_body(), 8192).await.unwrap()).unwrap();
        assert_eq!(json["version"], "1.2.3");
        assert!(
            json["platforms"]["windows-x86_64-nsis"]["url"]
                .as_str()
                .unwrap()
                .starts_with("https://updates.example/")
        );
        let head = app.clone().oneshot(request("HEAD", &path)).await.unwrap();
        assert_eq!(head.status(), StatusCode::OK);
        assert_eq!(head.headers()[header::CONTENT_LENGTH], "10");
        assert_eq!(
            head.headers()[header::CONTENT_DISPOSITION],
            "attachment; filename=\"installer.exe\""
        );
        let etag = head.headers()[header::ETAG].clone();
        assert!(to_bytes(head.into_body(), 100).await.unwrap().is_empty());
        let conditional = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(&path)
                    .header(header::IF_NONE_MATCH, etag)
                    .header(header::RANGE, "bytes=10-")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(conditional.status(), StatusCode::NOT_MODIFIED);
        let stale = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(&path)
                    .header(header::IF_RANGE, "\"old\"")
                    .header(header::RANGE, "bytes=2-4")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(stale.status(), StatusCode::OK);
        assert_eq!(&to_bytes(stale.into_body(), 100).await.unwrap()[..], bytes);
        let range = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(&path)
                    .header(header::RANGE, "bytes=2-4")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(range.status(), StatusCode::PARTIAL_CONTENT);
        assert_eq!(range.headers()[header::CONTENT_RANGE], "bytes 2-4/10");
        assert_eq!(&to_bytes(range.into_body(), 100).await.unwrap()[..], b"234");
        let invalid = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(&path)
                    .header(header::RANGE, "bytes=10-")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(invalid.status(), StatusCode::RANGE_NOT_SATISFIABLE);
        assert_eq!(invalid.headers()[header::CONTENT_RANGE], "bytes */10");
        store.withdraw(&package.id, "stable").await.unwrap();
        assert_eq!(
            app.clone()
                .oneshot(request("GET", &path))
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
        assert_eq!(
            app.oneshot(request("GET", "/mskdsp-upper/stable/latest.json"))
                .await
                .unwrap()
                .status(),
            StatusCode::NOT_FOUND
        );
    }

    // 平台候选仅能在管理员列表中查看，HTTP 下载和发布撤回均不可用。
    #[tokio::test]
    async fn platform_candidates_have_no_public_download_or_release_actions() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
        let upload = store
            .create_upload(super::super::models::PackageMetadata {
                product: "mskdsp-platform".into(),
                platform: "linux-amd64".into(),
                version: "0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa".into(),
                file_name: "platform-binary.tar.gz".into(),
                size: 3,
                sha256: format!("{:x}", Sha256::digest(b"abc")),
                source_ref: "main".into(),
                source_sha: "a".repeat(40),
                build_time: "2026-10-18T09:42:36Z".into(),
                channel: "ci".into(),
                ..Default::default()
            })
            .await
            .unwrap();
        store
            .append_chunk(&upload.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        let package = store.complete_upload(&upload.id).await.unwrap();
        let auth = HttpState {
            sessions: Default::default(),
            protocol_version: "test.v1".into(),
            auth: auth::AdminAuth::new(Default::default()).unwrap(),
        };
        let app = router(
            auth.clone(),
            store.clone(),
            "https://updates.example".into(),
        );
        let path = format!("/software/packages/{}/platform-binary.tar.gz", package.id);
        for method in ["GET", "HEAD"] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .method(method)
                        .uri(&path)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::NOT_FOUND);
        }
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "platform.example".parse().unwrap());
        headers.insert("X-MskDSP-Action", "software".parse().unwrap());
        let state = SoftwareState {
            auth,
            store: store.clone(),
            public_base_url: "https://updates.example".into(),
        };
        assert_eq!(
            publish(
                State(state.clone()),
                Path(package.id.clone()),
                headers.clone(),
                Ok(Json(Channel {
                    channel: "ci".into()
                }))
            )
            .await
            .status(),
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            withdraw(
                State(state),
                Path(package.id.clone()),
                headers,
                Ok(Json(Channel {
                    channel: "ci".into()
                }))
            )
            .await
            .status(),
            StatusCode::BAD_REQUEST
        );
        assert!(!store.get_package(&package.id).await.unwrap().ever_published);
    }

    #[tokio::test]
    async fn lower_manifests_and_checksums_are_public_only_after_publication() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
        let auth = HttpState {
            sessions: Default::default(),
            protocol_version: "test.v1".into(),
            auth: auth::AdminAuth::new(Default::default()).unwrap(),
        };
        let app = router(
            auth,
            store.clone(),
            "http://updates.example/download".into(),
        );
        for (platform, manifest_path) in [
            ("linux-arm64", "/mskdsp-lower/ci/latest.json"),
            ("linux-x64", "/mskdsp-lower-x64/ci/latest.json"),
        ] {
            let bytes = b"lower-archive";
            let metadata = super::super::models::PackageMetadata {
                product: "mskdsp-lower".into(),
                version: "1.2.3".into(),
                platform: platform.into(),
                file_name: "lower.tar.gz".into(),
                size: bytes.len() as u64,
                sha256: format!("{:x}", Sha256::digest(bytes)),
                package_version: "1.2.3+test".into(),
                image_id: format!("sha256:{}", "a".repeat(64)),
                ..Default::default()
            };
            let upload = store.create_upload(metadata).await.unwrap();
            store
                .append_chunk(&upload.id, 0, bytes.to_vec())
                .await
                .unwrap();
            let package = store.complete_upload(&upload.id).await.unwrap();
            let checksum_path = format!("/software/packages/{}/SHA256SUMS", package.id);
            let req = |method, uri: &str| {
                Request::builder()
                    .method(method)
                    .uri(uri)
                    .body(Body::empty())
                    .unwrap()
            };
            assert_eq!(
                app.clone()
                    .oneshot(req("GET", &checksum_path))
                    .await
                    .unwrap()
                    .status(),
                StatusCode::NOT_FOUND
            );
            store.publish(&package.id, "ci").await.unwrap();
            let manifest = app
                .clone()
                .oneshot(req("GET", manifest_path))
                .await
                .unwrap();
            assert_eq!(manifest.status(), StatusCode::OK);
            let json: Value =
                serde_json::from_slice(&to_bytes(manifest.into_body(), 8192).await.unwrap())
                    .unwrap();
            assert_eq!(json["schema_version"], 1);
            assert_eq!(json["platform"], platform);
            assert!(
                json["asset"]["url"]
                    .as_str()
                    .unwrap()
                    .starts_with("http://updates.example/download/software/packages/")
            );
            assert!(
                json["checksum"]["url"]
                    .as_str()
                    .unwrap()
                    .ends_with("/SHA256SUMS")
            );
            let checksum = app
                .clone()
                .oneshot(req("GET", &checksum_path))
                .await
                .unwrap();
            assert_eq!(checksum.status(), StatusCode::OK);
            let expected = format!("{:x}  lower.tar.gz\n", Sha256::digest(bytes));
            assert_eq!(
                &to_bytes(checksum.into_body(), 1024).await.unwrap()[..],
                expected.as_bytes()
            );
            let head = app
                .clone()
                .oneshot(req("HEAD", &checksum_path))
                .await
                .unwrap();
            assert_eq!(
                head.headers()[header::CONTENT_LENGTH],
                expected.len().to_string()
            );
            assert!(to_bytes(head.into_body(), 1024).await.unwrap().is_empty());
        }
    }

    #[tokio::test]
    async fn authenticated_publication_enforces_origin_action_json_and_configuration() {
        let directory = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(directory.path().into(), 1024).unwrap();
        let auth = HttpState {
            sessions: Default::default(),
            protocol_version: "test.v1".into(),
            auth: auth::AdminAuth::new(auth::AuthConfig {
                password_hash: auth::hash_password("test-password").unwrap(),
                ..Default::default()
            })
            .unwrap(),
        };
        let login_app = crate::http::router(auth.clone());
        let login = login_app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/auth/login")
                    .header(header::HOST, "platform.example")
                    .header(header::ORIGIN, "https://platform.example")
                    .header(header::CONTENT_TYPE, "application/json")
                    .header("X-MskDSP-Action", "auth")
                    .body(Body::from(
                        r#"{"username":"root","password":"test-password"}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(login.status(), StatusCode::OK);
        let cookie = login.headers()[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_string();
        let app = router(
            auth.clone(),
            store.clone(),
            "https://updates.example".into(),
        );
        let empty_base = router(auth, store.clone(), String::new());
        let upload = store
            .create_upload(super::super::models::PackageMetadata {
                product: "mskdsp-upper".into(),
                version: "1.2.3".into(),
                platform: "windows-x86_64-nsis".into(),
                file_name: "installer.exe".into(),
                size: 3,
                sha256: format!("{:x}", Sha256::digest(b"abc")),
                signature: "signed".into(),
                ..Default::default()
            })
            .await
            .unwrap();
        store
            .append_chunk(&upload.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        let package = store.complete_upload(&upload.id).await.unwrap();
        let path = format!("/api/v1/software/packages/{}/publish", package.id);
        let request = |action: &str, origin: &str, body: &str| {
            Request::builder()
                .method("POST")
                .uri(&path)
                .header(header::HOST, "platform.example")
                .header(header::ORIGIN, origin)
                .header(header::COOKIE, &cookie)
                .header(header::CONTENT_TYPE, "application/json")
                .header("X-MskDSP-Action", action)
                .body(Body::from(body.to_owned()))
                .unwrap()
        };
        assert_eq!(
            app.clone()
                .oneshot(request(
                    "",
                    "https://platform.example",
                    r#"{"channel":"stable"}"#
                ))
                .await
                .unwrap()
                .status(),
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            app.clone()
                .oneshot(request(
                    "software",
                    "https://other.example",
                    r#"{"channel":"stable"}"#
                ))
                .await
                .unwrap()
                .status(),
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            app.clone()
                .oneshot(request("software", "https://platform.example", "{"))
                .await
                .unwrap()
                .status(),
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            empty_base
                .oneshot(request(
                    "software",
                    "https://platform.example",
                    r#"{"channel":"stable"}"#
                ))
                .await
                .unwrap()
                .status(),
            StatusCode::PRECONDITION_FAILED
        );
        assert!(
            store
                .get_package(&package.id)
                .await
                .unwrap()
                .channels
                .is_empty()
        );
        let published = app
            .oneshot(request(
                "software",
                "https://platform.example",
                r#"{"channel":"stable"}"#,
            ))
            .await
            .unwrap();
        assert_eq!(published.status(), StatusCode::OK);
        assert_eq!(
            store.get_package(&package.id).await.unwrap().channels,
            vec!["stable"]
        );
    }
}
