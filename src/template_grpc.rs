pub mod proto {
    tonic::include_proto!("mskdsp.platform.templates.v1");
}

use crate::{
    client_auth::{ClientAuth, ClientAuthError},
    modbus_templates::{StoreError, TemplateStore},
};
use prost::Message;
use proto::template_catalog_server::{TemplateCatalog, TemplateCatalogServer};
use serde::Deserialize;
use tonic::{Request, Response, Status};

pub const MAX_GRPC_BYTES: usize = 12 * 1024 * 1024;

#[derive(Clone)]
pub struct TemplateCatalogService {
    store: TemplateStore,
    auth: ClientAuth,
}
impl TemplateCatalogService {
    pub fn new(store: TemplateStore, auth: ClientAuth) -> Self {
        Self { store, auth }
    }
    pub fn server(self) -> TemplateCatalogServer<Self> {
        TemplateCatalogServer::new(self)
            .max_decoding_message_size(4096)
            .max_encoding_message_size(MAX_GRPC_BYTES)
    }
    async fn authenticate<T>(&self, request: &Request<T>) -> Result<(), Status> {
        let token = crate::grpc::bearer_token(request.metadata())?;
        self.auth
            .authenticate(&token)
            .await
            .map_err(|error| match error {
                ClientAuthError::Storage => Status::unavailable("账号存储暂不可用"),
                _ => Status::unauthenticated("上位机登录状态无效，请重新登录"),
            })?;
        Ok(())
    }
}
fn storage(error: StoreError) -> Status {
    match error {
        StoreError::Invalid(message) => Status::invalid_argument(message),
        StoreError::NotFound => Status::not_found("平台模板或指定版本不存在"),
        StoreError::TooLarge | StoreError::Capacity => {
            Status::resource_exhausted(error.to_string())
        }
        _ => Status::unavailable("点表存储暂不可用"),
    }
}
#[derive(Deserialize)]
struct Summary {
    id: String,
    revision: u64,
    name: String,
    manufacturer: String,
    models: Vec<String>,
    protocol_version: String,
    point_count: u32,
    updated_at: u64,
}
fn bounded(text: &str, max: usize) -> bool {
    !text.contains('\0') && text.encode_utf16().count() <= max
}
fn summary(value: serde_json::Value) -> Result<proto::TemplateSummary, &'static str> {
    let item: Summary = serde_json::from_value(value).map_err(|_| "点表目录记录损坏")?;
    if !matches!(uuid::Uuid::parse_str(&item.id), Ok(id) if id.to_string() == item.id)
        || item.revision == 0
        || !(1..=10000).contains(&item.point_count)
        || item.name.trim().is_empty()
        || !bounded(&item.name, 256)
        || !bounded(&item.manufacturer, 256)
        || !bounded(&item.protocol_version, 256)
        || item.models.len() > 100
        || item
            .models
            .iter()
            .any(|model| model.trim().is_empty() || !bounded(model, 256))
    {
        return Err("点表目录记录损坏");
    }
    Ok(proto::TemplateSummary {
        id: item.id,
        revision: item.revision.to_string(),
        name: item.name,
        manufacturer: item.manufacturer,
        models: item.models,
        protocol_version: item.protocol_version,
        point_count: item.point_count,
        updated_at: item.updated_at,
    })
}
#[tonic::async_trait]
impl TemplateCatalog for TemplateCatalogService {
    async fn list_templates(
        &self,
        request: Request<proto::ListTemplatesRequest>,
    ) -> Result<Response<proto::ListTemplatesResponse>, Status> {
        self.authenticate(&request).await?;
        let records = self.store.list().await.map_err(storage)?;
        if records.len() > 1000 {
            return Err(Status::resource_exhausted("模板目录超过 1000 张上限"));
        }
        let response = proto::ListTemplatesResponse {
            templates: records
                .into_iter()
                .map(summary)
                .collect::<Result<_, _>>()
                .map_err(Status::data_loss)?,
        };
        if response.encoded_len() > MAX_GRPC_BYTES {
            return Err(Status::resource_exhausted("模板目录超过消息上限"));
        }
        Ok(Response::new(response))
    }
    async fn get_template(
        &self,
        request: Request<proto::GetTemplateRequest>,
    ) -> Result<Response<proto::TemplateRecord>, Status> {
        self.authenticate(&request).await?;
        let request = request.into_inner();
        let record = if request.revision.is_empty() {
            self.store.get(&request.id).await
        } else {
            let revision = request
                .revision
                .parse::<u64>()
                .ok()
                .filter(|value| *value > 0 && value.to_string() == request.revision)
                .ok_or_else(|| Status::invalid_argument("模板版本必须为正整数十进制字符串"))?;
            self.store.get_revision(&request.id, revision).await
        }
        .map_err(storage)?;
        let response = proto::TemplateRecord {
            id: record["id"]
                .as_str()
                .ok_or_else(|| Status::data_loss("点表记录损坏"))?
                .into(),
            revision: record["revision"]
                .as_u64()
                .ok_or_else(|| Status::data_loss("点表版本损坏"))?
                .to_string(),
            template_json: serde_json::to_string(&record["template"])
                .map_err(|_| Status::data_loss("点表内容损坏"))?,
            created_at: record["created_at"]
                .as_u64()
                .ok_or_else(|| Status::data_loss("点表时间损坏"))?,
            updated_at: record["updated_at"]
                .as_u64()
                .ok_or_else(|| Status::data_loss("点表时间损坏"))?,
        };
        Ok(Response::new(response))
    }
}

#[cfg(test)]
#[path = "template_grpc_tests.rs"]
mod tests;
