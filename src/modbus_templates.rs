use axum::http::StatusCode;
use serde_json::{Map, Value, json};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;
use uuid::Uuid;

const MAX_TEMPLATE_BYTES: usize = 2 * 1024 * 1024;
const MAX_RECORD_BYTES: usize = MAX_TEMPLATE_BYTES + 4096;
const MAX_TEMPLATES: usize = 100;
const TEMPLATE_FIELDS: &[&str] = &[
    "schema_version",
    "id",
    "name",
    "manufacturer",
    "models",
    "protocol_version",
    "address_base",
    "notes",
    "sources",
    "points",
];
const POINT_FIELDS: &[&str] = &[
    "id",
    "tag",
    "group",
    "unit",
    "function_code",
    "address",
    "data_type",
    "reg_count",
    "scale",
    "offset",
    "deadband",
    "word_order",
    "byte_order",
    "bit_index",
    "source_page",
    "description",
];

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("{0}")]
    Invalid(String),
    #[error("点表不存在")]
    NotFound,
    #[error("点表版本已变化，请重新读取后再保存或删除")]
    Conflict,
    #[error("模板文件不能超过 2 MiB")]
    TooLarge,
    #[error("点表库最多保存 100 张点表")]
    Capacity,
    #[error("{0}")]
    Storage(String),
}

impl StoreError {
    pub fn status(&self) -> StatusCode {
        match self {
            Self::Invalid(_) => StatusCode::BAD_REQUEST,
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Conflict | Self::Capacity => StatusCode::CONFLICT,
            Self::TooLarge => StatusCode::PAYLOAD_TOO_LARGE,
            Self::Storage(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }
}

fn invalid(message: impl Into<String>) -> StoreError {
    StoreError::Invalid(message.into())
}

fn object<'a>(
    value: &'a Value,
    fields: &[&str],
    optional: &[&str],
    label: &str,
) -> Result<&'a Map<String, Value>, StoreError> {
    let value = value
        .as_object()
        .ok_or_else(|| invalid(format!("{label}必须是对象")))?;
    for key in value.keys() {
        if !fields.contains(&key.as_str()) && !optional.contains(&key.as_str()) {
            return Err(invalid(format!("{label}包含未允许的字段：{key}")));
        }
    }
    for key in fields {
        if !value.contains_key(*key) {
            return Err(invalid(format!("{label}缺少字段：{key}")));
        }
    }
    Ok(value)
}

fn javascript_whitespace(c: char) -> bool {
    matches!(c, '\u{0009}'..='\u{000d}' | '\u{0020}' | '\u{00a0}' | '\u{1680}' | '\u{2000}'..='\u{200a}' | '\u{2028}' | '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}' | '\u{feff}')
}

fn text<'a>(value: &'a Value, label: &str, empty: bool, max: usize) -> Result<&'a str, StoreError> {
    let error = || invalid(format!("{label}必须是有效文本（最多 {max} 字符）"));
    let value = value.as_str().ok_or_else(error)?;
    if value.encode_utf16().count() > max
        || value.contains('\0')
        || (!empty && value.trim_matches(javascript_whitespace).is_empty())
    {
        return Err(error());
    }
    Ok(value)
}

fn integer(value: &Value, min: u64, max: u64, label: &str) -> Result<u64, StoreError> {
    let error = || invalid(format!("{label}必须是 {min} 到 {max} 的整数"));
    let number = value.as_f64().ok_or_else(error)?;
    if !number.is_finite() || number.fract() != 0.0 || number < min as f64 || number > max as f64 {
        return Err(error());
    }
    Ok(number as u64)
}

fn array<'a>(value: &'a Value, label: &str, max: usize) -> Result<&'a Vec<Value>, StoreError> {
    value
        .as_array()
        .filter(|items| items.len() <= max)
        .ok_or_else(|| invalid(format!("{label}必须是数组且最多 {max} 项")))
}

fn decimal(value: &Value, label: &str) -> Result<(), StoreError> {
    let value = value
        .as_str()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| invalid(format!("{label} 不能为空")))?;
    if value.len() > 4096 {
        return Err(invalid(format!("{label} 文本过长")));
    }
    let syntax_error = || {
        invalid(format!(
            "{label} 必须是完整十进制数，可使用科学计数法且不能包含空白"
        ))
    };
    let bytes = value.as_bytes();
    let mut position = usize::from(matches!(bytes[0], b'+' | b'-'));
    let integer_start = position;
    while position < bytes.len() && bytes[position].is_ascii_digit() {
        position += 1;
    }
    let integer_end = position;
    let mut fractional_start = position;
    if position < bytes.len() && bytes[position] == b'.' {
        position += 1;
        fractional_start = position;
        while position < bytes.len() && bytes[position].is_ascii_digit() {
            position += 1;
        }
    }
    let fractional_end = position;
    if integer_end == integer_start && fractional_end == fractional_start {
        return Err(syntax_error());
    }
    let mut exponent = 0i64;
    if position < bytes.len() && matches!(bytes[position], b'e' | b'E') {
        position += 1;
        let negative = position < bytes.len() && bytes[position] == b'-';
        if position < bytes.len() && matches!(bytes[position], b'+' | b'-') {
            position += 1;
        }
        let start = position;
        while position < bytes.len() && bytes[position].is_ascii_digit() {
            position += 1;
        }
        if start == position {
            return Err(syntax_error());
        }
        let magnitude = value[start..position].trim_start_matches('0');
        if magnitude.len() > 6 {
            return Err(invalid(format!(
                "{label} 的科学计数法指数绝对值不能超过 100000"
            )));
        }
        exponent = if magnitude.is_empty() {
            0
        } else {
            magnitude.parse::<i64>().map_err(|_| syntax_error())?
        };
        if exponent > 100000 {
            return Err(invalid(format!(
                "{label} 的科学计数法指数绝对值不能超过 100000"
            )));
        }
        if negative {
            exponent = -exponent;
        }
    }
    if position != bytes.len() {
        return Err(syntax_error());
    }
    let digits = format!(
        "{}{}",
        &value[integer_start..integer_end],
        &value[fractional_start..fractional_end]
    );
    let significant = digits.trim_start_matches('0');
    if !significant.is_empty()
        && significant.len() as i64 + exponent - (fractional_end - fractional_start) as i64 > 100
    {
        return Err(invalid(format!("{label} 的整数部分最多 100 位")));
    }
    if !value
        .parse::<f64>()
        .map_err(|_| syntax_error())?
        .is_finite()
    {
        return Err(invalid(format!("{label}必须为有限数值")));
    }
    Ok(())
}

pub fn validate_template(input: &Value) -> Result<(), StoreError> {
    let bytes =
        serde_json::to_vec(input).map_err(|error| invalid(format!("模板不能序列化：{error}")))?;
    if bytes.len() > MAX_TEMPLATE_BYTES {
        return Err(StoreError::TooLarge);
    }
    let exported_bytes = serde_json::to_vec_pretty(input)
        .map_err(|error| invalid(format!("模板不能序列化：{error}")))?;
    if exported_bytes.len() + 1 > MAX_TEMPLATE_BYTES {
        return Err(StoreError::TooLarge);
    }
    let data = object(input, TEMPLATE_FIELDS, &[], "模板")?;
    if integer(&data["schema_version"], 1, 1, "模板版本").is_err() {
        return Err(invalid("不支持的模板版本"));
    }
    let id = text(&data["id"], "模板 ID", false, 128)?;
    if !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b'-'))
    {
        return Err(invalid("模板 ID 仅支持字母、数字、下划线、点和连字符"));
    }
    let base = integer(&data["address_base"], 0, 1, "地址基准")?;
    let points = array(&data["points"], "模板点位", 2000)?;
    if points.is_empty() {
        return Err(invalid("模板至少需要一个点位"));
    }
    let mut ids = HashSet::new();
    let mut tags = HashSet::new();
    for (index, raw) in points.iter().enumerate() {
        let point = object(
            raw,
            POINT_FIELDS,
            &["default_selected", "condition", "enum_value"],
            &format!("第 {} 个点位", index + 1),
        )?;
        let point_id = text(&point["id"], "点位 ID", false, 128)?;
        let tag = text(&point["tag"], "点位标签", false, 256)?;
        if tag != tag.trim_matches(javascript_whitespace) {
            return Err(invalid("点位标签首尾不能包含空白"));
        }
        if !ids.insert(point_id) || !tags.insert(tag) {
            return Err(invalid("模板存在重复点位 ID 或标签"));
        }
        let fc = integer(&point["function_code"], 1, 16, "功能码")?;
        if !matches!(fc, 1 | 3 | 4 | 5 | 6 | 16) {
            return Err(invalid("模板包含不支持的功能码"));
        }
        let kind = point["data_type"]
            .as_str()
            .ok_or_else(|| invalid("模板包含不支持的数据类型"))?;
        if !matches!(kind, "BOOL" | "UINT16" | "INT16" | "UINT32" | "INT32") {
            return Err(invalid("模板包含不支持的数据类型"));
        }
        let count = integer(&point["reg_count"], 1, 2, "寄存器数量")?;
        let count_valid = match fc {
            1 | 5 => kind == "BOOL" && count == 1,
            6 => matches!(kind, "BOOL" | "UINT16" | "INT16") && count == 1,
            3 | 4 => match kind {
                "BOOL" => true,
                "UINT16" | "INT16" => count == 1,
                _ => count == 2,
            },
            16 => {
                kind != "BOOL"
                    && if matches!(kind, "UINT32" | "INT32") {
                        count == 2
                    } else {
                        count == 1
                    }
            }
            _ => false,
        };
        if !count_valid {
            return Err(invalid("功能码、数据类型和寄存器数量不匹配"));
        }
        integer(
            &point["address"],
            base,
            65535 + base - count + 1,
            "点位地址",
        )?;
        let bit = if point["bit_index"].is_null() {
            None
        } else {
            Some(integer(&point["bit_index"], 0, count * 16 - 1, "位索引")?)
        };
        if kind != "BOOL" && bit.is_some() {
            return Err(invalid("仅 BOOL 点位允许位索引"));
        }
        if matches!(fc, 1 | 5) && bit.is_some() {
            return Err(invalid("线圈点位不使用寄存器位索引"));
        }
        if let Some(value) = point.get("enum_value").filter(|value| !value.is_null()) {
            integer(
                value,
                0,
                if count == 1 { 65535 } else { 4294967295 },
                "枚举相等值",
            )?;
            if kind != "BOOL" || !matches!(fc, 3 | 4) || bit.is_some() {
                return Err(invalid(
                    "枚举相等值仅支持只读寄存器 BOOL，且不能同时配置位索引",
                ));
            }
        }
        if !matches!(point["word_order"].as_str(), Some("HL" | "LH"))
            || !matches!(point["byte_order"].as_str(), Some("AB" | "BA"))
        {
            return Err(invalid("字序或字节序不合法"));
        }
        if let Some(value) = point.get("default_selected") {
            if !value.is_boolean() {
                return Err(invalid("默认选择标记必须为布尔值"));
            }
        }
        if let Some(value) = point.get("condition") {
            text(value, "适用条件", true, 4096)?;
        }
        text(&point["group"], "分组", true, 256)?;
        text(&point["unit"], "单位", true, 64)?;
        text(&point["description"], "点位说明", true, 4096)?;
        integer(&point["source_page"], 0, 100000, "来源页码")?;
        for (field, label) in [("scale", "倍率"), ("offset", "偏移"), ("deadband", "死区")] {
            decimal(&point[field], label)?;
        }
    }
    text(&data["name"], "模板名称", false, 256)?;
    text(&data["manufacturer"], "厂家", true, 256)?;
    text(&data["protocol_version"], "协议版本", true, 256)?;
    for value in array(&data["models"], "型号", 100)? {
        text(value, "型号", false, 256)?;
    }
    for value in array(&data["notes"], "说明", 100)? {
        text(value, "说明", false, 4096)?;
    }
    for raw in array(&data["sources"], "来源", 20)? {
        let source = object(raw, &["file", "sha256"], &[], "来源")?;
        text(&source["file"], "来源文件名", false, 512)?;
        let hash = text(&source["sha256"], "来源 SHA256", false, 64)?;
        if hash.len() != 64 || !hash.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(invalid("来源 SHA256 格式不合法"));
        }
    }
    Ok(())
}

#[derive(Clone)]
pub struct TemplateStore {
    root: Arc<PathBuf>,
    lock: Arc<Mutex<()>>,
}

impl TemplateStore {
    pub fn new(root: PathBuf) -> Result<Self, StoreError> {
        fs::create_dir_all(&root)
            .map_err(|error| StoreError::Storage(format!("创建点表存储目录失败：{error}")))?;
        Ok(Self {
            root: Arc::new(root),
            lock: Arc::new(Mutex::new(())),
        })
    }

    async fn run<T, F>(&self, operation: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&Path) -> Result<T, StoreError> + Send + 'static,
    {
        let guard = self.lock.clone().lock_owned().await;
        let root = self.root.clone();
        tokio::task::spawn_blocking(move || {
            let _guard = guard;
            operation(&root)
        })
        .await
        .map_err(|error| StoreError::Storage(format!("点表存储任务失败：{error}")))?
    }

    pub async fn list(&self) -> Result<Vec<Value>, StoreError> {
        self.run(|root| {
            let mut records = read_records(root)?;
            records.sort_by(|left, right| {
                right["updated_at"].as_u64().cmp(&left["updated_at"].as_u64()).then_with(|| left["id"].as_str().cmp(&right["id"].as_str()))
            });
            Ok(records.into_iter().map(|record| {
                let template = &record["template"];
                json!({"id":record["id"],"revision":record["revision"],"name":template["name"],"manufacturer":template["manufacturer"],"models":template["models"],"protocol_version":template["protocol_version"],"address_base":template["address_base"],"point_count":template["points"].as_array().map_or(0, Vec::len),"created_at":record["created_at"],"updated_at":record["updated_at"]})
            }).collect())
        }).await
    }

    pub async fn get(&self, id: &str) -> Result<Value, StoreError> {
        valid_platform_id(id)?;
        let id = id.to_owned();
        self.run(move |root| read_record(root, &id)).await
    }

    pub async fn create(&self, template: Value) -> Result<Value, StoreError> {
        validate_template(&template)?;
        self.run(move |root| {
            if read_records(root)?.len() >= MAX_TEMPLATES { return Err(StoreError::Capacity); }
            let id = Uuid::new_v4().to_string();
            let timestamp = now_millis()?;
            let record = json!({"id":id,"revision":1,"created_at":timestamp,"updated_at":timestamp,"template":template});
            persist(root, &id, &record)?;
            Ok(record)
        }).await
    }

    pub async fn update(
        &self,
        id: &str,
        revision: u64,
        template: Value,
    ) -> Result<Value, StoreError> {
        valid_platform_id(id)?;
        validate_template(&template)?;
        let id = id.to_owned();
        self.run(move |root| {
            let old = read_record(root, &id)?;
            if old["revision"].as_u64() != Some(revision) { return Err(StoreError::Conflict); }
            let next = revision.checked_add(1).ok_or_else(|| StoreError::Storage("点表版本号已达到上限".into()))?;
            let timestamp = now_millis()?.max(old["updated_at"].as_u64().unwrap());
            let record = json!({"id":id,"revision":next,"created_at":old["created_at"],"updated_at":timestamp,"template":template});
            persist(root, &id, &record)?;
            Ok(record)
        }).await
    }

    pub async fn delete(&self, id: &str, revision: u64) -> Result<(), StoreError> {
        valid_platform_id(id)?;
        let id = id.to_owned();
        self.run(move |root| {
            let old = read_record(root, &id)?;
            if old["revision"].as_u64() != Some(revision) {
                return Err(StoreError::Conflict);
            }
            fs::remove_file(root.join(format!("{id}.json")))
                .map_err(|error| StoreError::Storage(format!("删除点表文件失败：{error}")))?;
            sync_directory(root)
        })
        .await
    }
}

fn valid_platform_id(id: &str) -> Result<(), StoreError> {
    if !matches!(Uuid::parse_str(id), Ok(uuid) if uuid.to_string() == id) {
        return Err(invalid("平台点表 ID 必须是标准小写 UUID"));
    }
    Ok(())
}

fn now_millis() -> Result<u64, StoreError> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| StoreError::Storage(format!("读取系统时间失败：{error}")))?;
    u64::try_from(elapsed.as_millis())
        .map_err(|_| StoreError::Storage("系统时间超出支持范围".into()))
}

fn read_records(root: &Path) -> Result<Vec<Value>, StoreError> {
    let mut records = Vec::new();
    for entry in fs::read_dir(root)
        .map_err(|error| StoreError::Storage(format!("读取点表存储目录失败：{error}")))?
    {
        let entry = entry
            .map_err(|error| StoreError::Storage(format!("读取点表存储目录项失败：{error}")))?;
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let id = path
            .file_stem()
            .and_then(|value| value.to_str())
            .ok_or_else(|| StoreError::Storage("点表存储文件名不合法".into()))?;
        valid_platform_id(id)
            .map_err(|_| StoreError::Storage("点表存储文件名不是标准 UUID".into()))?;
        records.push(read_record(root, id)?);
        if records.len() > MAX_TEMPLATES {
            return Err(StoreError::Storage(
                "点表存储文件数量超过 100 张上限".into(),
            ));
        }
    }
    Ok(records)
}

fn read_record(root: &Path, id: &str) -> Result<Value, StoreError> {
    let path = root.join(format!("{id}.json"));
    let metadata = fs::symlink_metadata(&path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            StoreError::NotFound
        } else {
            StoreError::Storage(format!("读取点表文件信息失败：{error}"))
        }
    })?;
    if !metadata.file_type().is_file() || metadata.len() > MAX_RECORD_BYTES as u64 {
        return Err(StoreError::Storage("点表存储文件损坏或不是普通文件".into()));
    }
    let mut bytes = Vec::new();
    File::open(&path)
        .and_then(|file| {
            file.take((MAX_RECORD_BYTES + 1) as u64)
                .read_to_end(&mut bytes)
        })
        .map_err(|error| StoreError::Storage(format!("读取点表文件失败：{error}")))?;
    if bytes.len() > MAX_RECORD_BYTES {
        return Err(StoreError::Storage("点表存储文件大小超出上限".into()));
    }
    let record: Value = serde_json::from_slice(&bytes)
        .map_err(|error| StoreError::Storage(format!("点表存储文件不是合法 JSON：{error}")))?;
    let verify = || -> Result<(), StoreError> {
        let fields = object(
            &record,
            &["id", "revision", "created_at", "updated_at", "template"],
            &[],
            "点表记录",
        )?;
        if fields["id"].as_str() != Some(id) {
            return Err(invalid("点表记录 ID 与文件名不一致"));
        }
        if fields["revision"]
            .as_u64()
            .filter(|value| *value > 0)
            .is_none()
        {
            return Err(invalid("点表记录版本号不合法"));
        }
        let created = fields["created_at"]
            .as_u64()
            .ok_or_else(|| invalid("点表记录创建时间不合法"))?;
        let updated = fields["updated_at"]
            .as_u64()
            .ok_or_else(|| invalid("点表记录更新时间不合法"))?;
        if updated < created {
            return Err(invalid("点表记录更新时间早于创建时间"));
        }
        validate_template(&fields["template"])
    };
    verify().map_err(|error| StoreError::Storage(format!("点表存储记录损坏：{error}")))?;
    Ok(record)
}

fn persist(root: &Path, id: &str, record: &Value) -> Result<(), StoreError> {
    let target = root.join(format!("{id}.json"));
    let temporary = root.join(format!(".{id}.{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<(), StoreError> {
        let bytes = serde_json::to_vec(record)
            .map_err(|error| StoreError::Storage(format!("序列化点表记录失败：{error}")))?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
            .map_err(|error| StoreError::Storage(format!("创建点表临时文件失败：{error}")))?;
        file.write_all(&bytes)
            .and_then(|()| file.sync_all())
            .map_err(|error| StoreError::Storage(format!("写入并同步点表文件失败：{error}")))?;
        drop(file);
        fs::rename(&temporary, &target)
            .map_err(|error| StoreError::Storage(format!("原子替换点表文件失败：{error}")))?;
        sync_directory(root)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn sync_directory(root: &Path) -> Result<(), StoreError> {
    #[cfg(unix)]
    File::open(root)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| StoreError::Storage(format!("同步点表存储目录失败：{error}")))?;
    #[cfg(not(unix))]
    let _ = root;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn template() -> Value {
        json!({"schema_version":1,"id":"demo.test-1","name":"测试点表","manufacturer":"","models":[],"protocol_version":"","address_base":0,"notes":[],"sources":[],"points":[{"id":"p1","tag":"电压","group":"","unit":"V","function_code":3,"address":0,"data_type":"UINT16","reg_count":1,"scale":"0.000000000000000000001","offset":"-1.5e-20","deadband":"0","word_order":"HL","byte_order":"AB","bit_index":null,"source_page":0,"description":""}]})
    }

    struct TestDirectory(PathBuf);
    impl TestDirectory {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("mskdsp-templates-{}", uuid::Uuid::new_v4()));
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }
        fn store(&self) -> TemplateStore {
            TemplateStore::new(self.0.clone()).unwrap()
        }
    }
    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn validates_full_upper_template_and_preserves_optional_fields() {
        let mut data = template();
        data["points"][0]["data_type"] = json!("BOOL");
        data["points"][0]["reg_count"] = json!(2);
        data["points"][0]["enum_value"] = json!(4294967295u64);
        data["points"][0]["condition"] = json!("机型 A 且固件版本 >= 3");
        data["points"][0]["default_selected"] = json!(false);
        data["sources"] = json!([{"file":"原始点表.pdf","sha256":"a".repeat(64)}]);
        validate_template(&data).unwrap();
    }

    #[test]
    fn validates_every_function_type_and_register_combination() {
        for fc in [1, 3, 4, 5, 6, 16] {
            for kind in ["BOOL", "UINT16", "INT16", "UINT32", "INT32"] {
                for count in [1, 2] {
                    let mut data = template();
                    data["points"][0]["function_code"] = json!(fc);
                    data["points"][0]["data_type"] = json!(kind);
                    data["points"][0]["reg_count"] = json!(count);
                    let expected = match fc {
                        1 | 5 => kind == "BOOL" && count == 1,
                        6 => matches!(kind, "BOOL" | "UINT16" | "INT16") && count == 1,
                        3 | 4 => match kind {
                            "BOOL" => true,
                            "UINT16" | "INT16" => count == 1,
                            _ => count == 2,
                        },
                        16 => {
                            kind != "BOOL"
                                && if matches!(kind, "UINT32" | "INT32") {
                                    count == 2
                                } else {
                                    count == 1
                                }
                        }
                        _ => false,
                    };
                    assert_eq!(
                        validate_template(&data).is_ok(),
                        expected,
                        "功能码 {fc} 类型 {kind} 数量 {count}"
                    );
                }
            }
        }
    }

    #[test]
    fn decimal_syntax_and_magnitude_match_upper_validation() {
        for valid in [
            "+1.",
            ".5",
            "-.5E+2",
            "0001.20e-2",
            "1e-100000",
            "0e100000",
            "1e99",
        ] {
            let mut data = template();
            data["points"][0]["scale"] = json!(valid);
            validate_template(&data).unwrap();
        }
        for invalid in [
            "",
            " 1",
            "1 ",
            "0x10",
            "NaN",
            "Infinity",
            "1e",
            "1e100",
            "0e100001",
            "1e-100001",
            "1_000",
        ] {
            let mut data = template();
            data["points"][0]["scale"] = json!(invalid);
            assert!(validate_template(&data).is_err(), "意外通过：{invalid}");
        }
        let mut data = template();
        data["points"][0]["offset"] = json!(1);
        assert!(validate_template(&data).is_err());
    }

    #[test]
    fn rejects_unknown_missing_duplicate_and_invalid_optional_fields() {
        let original = template();
        let mut cases = Vec::new();
        let mut data = original.clone();
        data["unknown"] = json!(true);
        cases.push(data);
        let mut data = original.clone();
        data.as_object_mut().unwrap().remove("notes");
        cases.push(data);
        let mut data = original.clone();
        data["schema_version"] = json!(2);
        cases.push(data);
        let mut data = original.clone();
        data["points"] = json!([]);
        cases.push(data);
        let mut data = original.clone();
        data["points"]
            .as_array_mut()
            .unwrap()
            .push(original["points"][0].clone());
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["condition"] = json!(false);
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["default_selected"] = json!(0);
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["enum_value"] = json!(1);
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["data_type"] = json!("BOOL");
        data["points"][0]["bit_index"] = json!(0);
        data["points"][0]["enum_value"] = json!(0);
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["tag"] = json!(" 电压");
        cases.push(data);
        let mut data = original.clone();
        data["id"] = json!("../unsafe");
        cases.push(data);
        let mut data = original.clone();
        data["points"][0]["description"] = json!("\0");
        cases.push(data);
        for data in cases {
            assert!(validate_template(&data).is_err(), "意外通过：{data}");
        }
    }

    #[test]
    fn address_and_bit_ranges_follow_base_and_register_width() {
        let mut data = template();
        data["address_base"] = json!(1);
        data["points"][0]["address"] = json!(65536);
        validate_template(&data).unwrap();
        data["points"][0]["address"] = json!(0);
        assert!(validate_template(&data).is_err());
        data["points"][0]["address"] = json!(65535);
        data["points"][0]["data_type"] = json!("BOOL");
        data["points"][0]["reg_count"] = json!(2);
        data["points"][0]["bit_index"] = json!(31);
        validate_template(&data).unwrap();
        data["points"][0]["bit_index"] = json!(32);
        assert!(validate_template(&data).is_err());
        data["points"][0]["bit_index"] = Value::Null;
        data["points"][0]["address"] = json!(65536);
        assert!(validate_template(&data).is_err());
    }

    #[test]
    fn text_lengths_use_javascript_utf16_units() {
        let mut data = template();
        data["name"] = json!("😀".repeat(128));
        validate_template(&data).unwrap();
        data["name"] = json!("😀".repeat(129));
        assert!(validate_template(&data).is_err());
        data["name"] = json!("\u{feff}");
        assert!(validate_template(&data).is_err());
    }

    #[test]
    fn rejects_point_limit_and_byte_limit() {
        let mut data = template();
        let point = data["points"][0].clone();
        data["points"] = json!(vec![point.clone(); 2001]);
        assert!(validate_template(&data).is_err());
        let mut points = Vec::new();
        for index in 0..600 {
            let mut next = point.clone();
            next["id"] = json!(format!("p{index}"));
            next["tag"] = json!(format!("tag{index}"));
            next["description"] = json!("x".repeat(4096));
            points.push(next);
        }
        data["points"] = json!(points);
        assert_eq!(
            validate_template(&data).unwrap_err().status(),
            axum::http::StatusCode::PAYLOAD_TOO_LARGE
        );
    }

    #[test]
    fn rejects_compact_template_when_formatted_download_exceeds_import_limit() {
        let mut data = template();
        let point = data["points"][0].clone();
        let mut points = Vec::new();
        for index in 0..500 {
            let mut next = point.clone();
            next["id"] = json!(format!("p{index}"));
            next["tag"] = json!(format!("tag{index}"));
            points.push(next);
        }
        data["points"] = json!(points);
        let overhead = serde_json::to_vec(&data).unwrap().len();
        let description_length = (MAX_TEMPLATE_BYTES - 1024 - overhead) / 500;
        assert!(description_length <= 4096);
        for point in data["points"].as_array_mut().unwrap() {
            point["description"] = json!("x".repeat(description_length));
        }
        assert!(serde_json::to_vec(&data).unwrap().len() <= MAX_TEMPLATE_BYTES);
        assert!(serde_json::to_vec_pretty(&data).unwrap().len() + 1 > MAX_TEMPLATE_BYTES);
        assert_eq!(
            validate_template(&data).unwrap_err().status(),
            axum::http::StatusCode::PAYLOAD_TOO_LARGE
        );
    }

    #[tokio::test]
    async fn persists_full_record_and_lists_only_summary_after_reopen() {
        let directory = TestDirectory::new();
        let record = directory.store().create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        assert_eq!(record["revision"], 1);
        assert_eq!(record["template"], template());
        let reopened = directory.store();
        assert_eq!(reopened.get(id).await.unwrap(), record);
        let list = reopened.list().await.unwrap();
        assert_eq!(list.len(), 1);
        assert_eq!(list[0]["point_count"], 1);
        assert_eq!(list[0]["name"], "测试点表");
        assert!(list[0].get("template").is_none());
        let duplicate = reopened.create(template()).await.unwrap();
        assert_ne!(duplicate["id"], record["id"]);
    }

    #[tokio::test]
    async fn serializes_concurrent_updates_and_rejects_stale_delete() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        let mut first = template();
        first["name"] = json!("版本 A");
        let mut second = template();
        second["name"] = json!("版本 B");
        let (left, right) = tokio::join!(store.update(id, 1, first), store.update(id, 1, second));
        assert_ne!(left.is_ok(), right.is_ok());
        let error = if let Err(error) = left {
            error
        } else {
            right.unwrap_err()
        };
        assert_eq!(error.status(), axum::http::StatusCode::CONFLICT);
        assert_eq!(store.get(id).await.unwrap()["revision"], 2);
        assert_eq!(
            store.delete(id, 1).await.unwrap_err().status(),
            axum::http::StatusCode::CONFLICT
        );
        store.delete(id, 2).await.unwrap();
        assert_eq!(
            store.get(id).await.unwrap_err().status(),
            axum::http::StatusCode::NOT_FOUND
        );
    }

    #[tokio::test]
    async fn rejects_unsafe_ids_and_reports_corruption_without_erasing_it() {
        let directory = TestDirectory::new();
        let store = directory.store();
        assert_eq!(
            store.get("../outside").await.unwrap_err().status(),
            axum::http::StatusCode::BAD_REQUEST
        );
        let path = directory.0.join(format!("{}.json", uuid::Uuid::new_v4()));
        std::fs::write(&path, b"{broken").unwrap();
        assert_eq!(
            store.list().await.unwrap_err().status(),
            axum::http::StatusCode::INTERNAL_SERVER_ERROR
        );
        assert!(store.create(template()).await.is_err());
        assert_eq!(std::fs::read(path).unwrap(), b"{broken");
    }

    #[tokio::test]
    async fn enforces_capacity_without_overwriting_duplicate_names() {
        let directory = TestDirectory::new();
        let store = directory.store();
        for _ in 0..100 {
            store.create(template()).await.unwrap();
        }
        assert_eq!(
            store.create(template()).await.unwrap_err().status(),
            axum::http::StatusCode::CONFLICT
        );
        assert_eq!(store.list().await.unwrap().len(), 100);
    }

    #[tokio::test]
    async fn refuses_storage_path_that_is_not_a_regular_file() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        let path = directory.0.join(format!("{id}.json"));
        std::fs::remove_file(&path).unwrap();
        std::fs::create_dir(&path).unwrap();
        assert_eq!(
            store.update(id, 1, template()).await.unwrap_err().status(),
            axum::http::StatusCode::INTERNAL_SERVER_ERROR
        );
    }

    #[tokio::test]
    async fn cancelled_request_keeps_lock_until_blocking_write_finishes() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let first_store = store.clone();
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let first = tokio::spawn(async move {
            first_store
                .run(move |_| {
                    let _ = started_tx.send(());
                    release_rx.recv().unwrap();
                    Ok(())
                })
                .await
        });
        started_rx.await.unwrap();
        first.abort();
        let second_store = store.clone();
        let (second_tx, mut second_rx) = tokio::sync::oneshot::channel();
        let second = tokio::spawn(async move {
            second_store
                .run(move |_| {
                    let _ = second_tx.send(());
                    Ok(())
                })
                .await
        });
        let early =
            tokio::time::timeout(std::time::Duration::from_millis(30), &mut second_rx).await;
        release_tx.send(()).unwrap();
        assert!(early.is_err(), "请求取消后不应提前释放写锁");
        tokio::time::timeout(std::time::Duration::from_secs(2), second_rx)
            .await
            .unwrap()
            .unwrap();
        second.await.unwrap().unwrap();
    }
}
