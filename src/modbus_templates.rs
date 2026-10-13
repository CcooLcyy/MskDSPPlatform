use axum::http::StatusCode;
use serde_json::{Map, Value, json};
use std::{
    collections::HashSet,
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::Mutex;
use uuid::Uuid;

#[path = "modbus_templates_sqlite.rs"]
mod sqlite;

const MAX_TEMPLATE_BYTES: usize = 10 * 1024 * 1024;
const MAX_RECORD_BYTES: usize = MAX_TEMPLATE_BYTES + 4096;
const MAX_TEMPLATES: usize = 1000;
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
    #[error("模板文件不能超过 10 MiB")]
    TooLarge,
    #[error("点表库最多保存 1000 张点表")]
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
    let points = array(&data["points"], "模板点位", 10000)?;
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
        F: FnOnce(&mut rusqlite::Connection) -> Result<T, StoreError> + Send + 'static,
    {
        let guard = self.lock.clone().lock_owned().await;
        let root = self.root.clone();
        tokio::task::spawn_blocking(move || {
            let _guard = guard;
            let mut database = sqlite::open_initialized(&root)?;
            operation(&mut database)
        })
        .await
        .map_err(|error| StoreError::Storage(format!("点表存储任务失败：{error}")))?
    }

    pub async fn initialize(&self) -> Result<(), StoreError> {
        self.purge_expired().await.map(|_| ())
    }

    pub async fn list(&self) -> Result<Vec<Value>, StoreError> {
        self.run(sqlite::list).await
    }

    pub async fn get(&self, id: &str) -> Result<Value, StoreError> {
        valid_platform_id(id)?;
        let id = id.to_owned();
        self.run(move |database| sqlite::get(database, &id)).await
    }

    pub async fn create(&self, template: Value) -> Result<Value, StoreError> {
        validate_template(&template)?;
        self.run(move |database| sqlite::create(database, template))
            .await
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
        self.run(move |database| sqlite::update(database, &id, revision, template))
            .await
    }

    pub async fn delete(&self, id: &str, revision: u64) -> Result<(), StoreError> {
        valid_platform_id(id)?;
        let id = id.to_owned();
        self.run(move |database| sqlite::delete(database, &id, revision))
            .await
    }

    pub async fn list_trash(&self) -> Result<Vec<Value>, StoreError> {
        self.run(sqlite::list_trash).await
    }

    pub async fn restore(&self, id: &str, revision: u64) -> Result<Value, StoreError> {
        valid_platform_id(id)?;
        let id = id.to_owned();
        self.run(move |database| sqlite::restore(database, &id, revision))
            .await
    }

    pub async fn purge_expired(&self) -> Result<usize, StoreError> {
        self.run(sqlite::purge_expired).await
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

#[cfg(test)]
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
                "点表存储文件数量超过 1000 张上限".into(),
            ));
        }
    }
    Ok(records)
}

#[cfg(test)]
fn read_record(root: &Path, id: &str) -> Result<Value, StoreError> {
    read_record_with_bytes(root, id).map(|(record, _)| record)
}

fn read_record_with_bytes(root: &Path, id: &str) -> Result<(Value, Vec<u8>), StoreError> {
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
    Ok((record, bytes))
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
        data["points"] = json!(vec![point.clone(); 10001]);
        assert!(validate_template(&data).is_err());
        let mut points = Vec::new();
        for index in 0..2600 {
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
    fn accepts_ten_thousand_unique_points_within_file_limit() {
        let mut data = template();
        let point = data["points"][0].clone();
        let points: Vec<_> = (0..10000)
            .map(|index| {
                let mut next = point.clone();
                next["id"] = json!(format!("p{index}"));
                next["tag"] = json!(format!("tag{index}"));
                next
            })
            .collect();
        data["points"] = json!(points);
        validate_template(&data).unwrap();
    }

    #[test]
    fn rejects_compact_template_when_formatted_download_exceeds_import_limit() {
        let mut data = template();
        let point = data["points"][0].clone();
        let mut points = Vec::new();
        for index in 0..2600 {
            let mut next = point.clone();
            next["id"] = json!(format!("p{index}"));
            next["tag"] = json!(format!("tag{index}"));
            points.push(next);
        }
        data["points"] = json!(points);
        let overhead = serde_json::to_vec(&data).unwrap().len();
        let description_length = (MAX_TEMPLATE_BYTES - 1024 - overhead) / 2600;
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
        let independent = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        let mut first = template();
        first["name"] = json!("版本 A");
        let mut second = template();
        second["name"] = json!("版本 B");
        let (left, right) = tokio::join!(
            store.update(id, 1, first),
            independent.update(id, 1, second)
        );
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
    async fn deleted_record_moves_to_trash_and_restores_full_data_with_new_revision() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        store.delete(id, 1).await.unwrap();
        assert!(store.list().await.unwrap().is_empty());
        assert!(matches!(store.get(id).await, Err(StoreError::NotFound)));
        let trash = store.list_trash().await.unwrap();
        assert_eq!(trash.len(), 1);
        assert_eq!(trash[0]["id"], id);
        assert_eq!(trash[0]["revision"], 2);
        assert_eq!(
            trash[0]["expires_at"].as_u64().unwrap() - trash[0]["deleted_at"].as_u64().unwrap(),
            7_776_000_000
        );
        assert!(trash[0].get("template").is_none());
        assert!(matches!(
            store.restore(id, 1).await,
            Err(StoreError::Conflict)
        ));
        let restored = directory.store().restore(id, 2).await.unwrap();
        assert_eq!(restored["id"], record["id"]);
        assert_eq!(restored["created_at"], record["created_at"]);
        assert_eq!(restored["revision"], 3);
        assert_eq!(restored["template"], record["template"]);
        assert!(store.list_trash().await.unwrap().is_empty());
        store.delete(id, 3).await.unwrap();
        assert_eq!(store.list_trash().await.unwrap()[0]["revision"], 4);
    }

    #[tokio::test]
    async fn trash_ninety_day_expiration_is_exact_and_expired_restore_purges_record() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap().to_owned();
        store
            .run(move |database| {
                let deleted_at = 1700000000000;
                let expires_at = deleted_at + 7_776_000_000;
                sqlite::delete_at(database, &id, 1, deleted_at)?;
                assert_eq!(sqlite::list_trash_at(database, expires_at - 1)?.len(), 1);
                assert!(matches!(
                    sqlite::restore_at(database, &id, 2, expires_at),
                    Err(StoreError::NotFound)
                ));
                let count: usize = database
                    .query_row("SELECT COUNT(*) FROM trash", [], |row| row.get(0))
                    .unwrap();
                assert_eq!(
                    count, 0,
                    "到期恢复请求必须实际永久清理，不能因404回滚清理事务"
                );
                Ok(())
            })
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn trash_overflow_refuses_delete_without_losing_active_record() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap().to_owned();
        store
            .run(move |database| {
                assert!(matches!(
                    sqlite::delete_at(database, &id, 1, u64::MAX - 7_776_000_000 + 1),
                    Err(StoreError::Storage(_))
                ));
                assert_eq!(sqlite::get(database, &id)?["revision"], 1);
                Ok(())
            })
            .await
            .unwrap();
        assert!(store.list_trash().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn future_updated_time_does_not_extend_retention_and_restore_keeps_it() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let mut record = legacy_record(&id);
        record["created_at"] = json!(u64::MAX - 1);
        record["updated_at"] = json!(u64::MAX);
        fs::write(
            directory.0.join(format!("{id}.json")),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        let store = directory.store();
        store.delete(&id, 7).await.unwrap();
        let trash = store.list_trash().await.unwrap();
        assert_eq!(
            trash[0]["expires_at"].as_u64().unwrap() - trash[0]["deleted_at"].as_u64().unwrap(),
            7_776_000_000
        );
        assert!(trash[0]["deleted_at"].as_u64().unwrap() < u64::MAX - 7_776_000_000);
        assert_eq!(trash[0]["updated_at"], u64::MAX);
        let restored = store.restore(&id, 8).await.unwrap();
        assert_eq!(restored["updated_at"], u64::MAX);
        assert_eq!(restored["revision"], 9);
        assert_eq!(restored["template"], record["template"]);
    }

    #[tokio::test]
    async fn restore_just_before_expiration_succeeds_and_explicit_and_startup_purge_remove_expired()
    {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap().to_owned();
        let saved_id = id.clone();
        store
            .run(move |database| {
                let deleted_at = 1700000000000;
                sqlite::delete_at(database, &id, 1, deleted_at)?;
                let restored =
                    sqlite::restore_at(database, &id, 2, deleted_at + 7_776_000_000 - 1)?;
                assert_eq!(restored["revision"], 3);
                sqlite::delete_at(database, &id, 3, deleted_at)?;
                Ok(())
            })
            .await
            .unwrap();
        assert_eq!(store.purge_expired().await.unwrap(), 1);
        assert_eq!(store.purge_expired().await.unwrap(), 0);
        assert!(matches!(
            store.restore(&saved_id, 4).await,
            Err(StoreError::NotFound)
        ));
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap().to_owned();
        store
            .run(move |database| sqlite::delete_at(database, &id, 1, 1700000000000))
            .await
            .unwrap();
        directory.store().initialize().await.unwrap();
        store
            .run(|database| {
                let count: usize = database
                    .query_row("SELECT COUNT(*) FROM trash", [], |row| row.get(0))
                    .unwrap();
                assert_eq!(count, 0, "启动初始化必须永久清理已过期回收站记录");
                Ok(())
            })
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn independent_stores_cannot_restore_same_record_twice() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let independent = directory.store();
        let record = store.create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        store.delete(id, 1).await.unwrap();
        let (left, right) = tokio::join!(store.restore(id, 2), independent.restore(id, 2));
        assert_ne!(left.is_ok(), right.is_ok());
        let error = left.err().or_else(|| right.err()).unwrap();
        assert!(matches!(error, StoreError::NotFound));
        assert_eq!(store.get(id).await.unwrap()["revision"], 3);
        assert!(store.list_trash().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn simultaneous_restore_respects_last_active_slot_and_preserves_failed_trash() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let left = store.create(template()).await.unwrap();
        let right = store.create(template()).await.unwrap();
        let left_id = left["id"].as_str().unwrap();
        let right_id = right["id"].as_str().unwrap();
        store.delete(left_id, 1).await.unwrap();
        store.delete(right_id, 1).await.unwrap();
        store.create(template()).await.unwrap();
        store.run(|database| {
            let transaction = database.transaction().unwrap();
            for _ in 1..MAX_TEMPLATES - 1 {
                transaction.execute("INSERT INTO templates SELECT ?1,revision,created_at,updated_at,name,manufacturer,models_json,protocol_version,address_base,point_count,template_json FROM templates LIMIT 1", [Uuid::new_v4().to_string()]).unwrap();
            }
            transaction.commit().unwrap();
            Ok(())
        }).await.unwrap();
        let independent = directory.store();
        let (left, right) =
            tokio::join!(store.restore(left_id, 2), independent.restore(right_id, 2));
        assert_ne!(left.is_ok(), right.is_ok());
        assert!(matches!(
            left.err().or_else(|| right.err()).unwrap(),
            StoreError::Capacity
        ));
        assert_eq!(store.list().await.unwrap().len(), MAX_TEMPLATES);
        assert_eq!(store.list_trash().await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn restore_storage_failure_rolls_back_insert_and_keeps_trash_while_committing_expiration_cleanup()
     {
        let directory = TestDirectory::new();
        let store = directory.store();
        let target = store.create(template()).await.unwrap();
        let expired = store.create(template()).await.unwrap();
        let target_id = target["id"].as_str().unwrap().to_owned();
        let expired_id = expired["id"].as_str().unwrap().to_owned();
        store.delete(&target_id, 1).await.unwrap();
        let trigger_id = target_id.clone();
        store.run(move |database| {
            sqlite::delete_at(database, &expired_id, 1, 1700000000000)?;
            database.execute_batch(&format!("CREATE TRIGGER restore_failure BEFORE DELETE ON trash WHEN OLD.id='{trigger_id}' BEGIN SELECT RAISE(ABORT,'模拟恢复存储故障'); END;")).unwrap();
            Ok(())
        }).await.unwrap();
        assert!(matches!(
            store.restore(&target_id, 2).await,
            Err(StoreError::Storage(_))
        ));
        assert!(matches!(
            store.get(&target_id).await,
            Err(StoreError::NotFound)
        ));
        store
            .run(|database| {
                let count: usize = database
                    .query_row("SELECT COUNT(*) FROM trash", [], |row| row.get(0))
                    .unwrap();
                assert_eq!(count, 1, "存储故障必须回滚恢复插入，同时提交已过期记录清理");
                database
                    .execute("DROP TRIGGER restore_failure", [])
                    .unwrap();
                Ok(())
            })
            .await
            .unwrap();
        let trash = store.list_trash().await.unwrap();
        assert_eq!(trash[0]["revision"], 2);
        let restored = store.restore(&target_id, 2).await.unwrap();
        assert_eq!(restored["template"], target["template"]);
        assert_eq!(restored["revision"], 3);
    }

    #[tokio::test]
    async fn database_v1_upgrade_preserves_rows_and_v2_missing_trash_is_rejected() {
        let directory = TestDirectory::new();
        let record = directory.store().create(template()).await.unwrap();
        let id = record["id"].as_str().unwrap();
        {
            let database =
                rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
            database.execute("DROP TABLE trash", []).unwrap();
            database.pragma_update(None, "user_version", 1).unwrap();
        }
        let reopened = directory.store();
        reopened.initialize().await.unwrap();
        assert_eq!(reopened.get(id).await.unwrap(), record);
        assert!(reopened.list_trash().await.unwrap().is_empty());
        {
            let database =
                rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
            let version: u32 = database
                .pragma_query_value(None, "user_version", |row| row.get(0))
                .unwrap();
            assert_eq!(version, 2);
            database.execute("DROP TABLE trash", []).unwrap();
        }
        assert!(reopened.initialize().await.is_err());
        let database = rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
        let tables: u32 = database
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='trash'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tables, 0, "不能把v2回收站缺表的数据损坏自动修成空回收站");
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
        store.create(template()).await.unwrap();
        store.run(|database| {
            let transaction = database.transaction().unwrap();
            for _ in 1..MAX_TEMPLATES - 1 {
                transaction.execute("INSERT INTO templates SELECT ?1,revision,created_at,updated_at,name,manufacturer,models_json,protocol_version,address_base,point_count,template_json FROM templates LIMIT 1", [Uuid::new_v4().to_string()]).unwrap();
            }
            transaction.commit().unwrap();
            Ok(())
        }).await.unwrap();
        let independent = directory.store();
        let (left, right) = tokio::join!(store.create(template()), independent.create(template()));
        assert_ne!(
            left.is_ok(),
            right.is_ok(),
            "跨存储实例只允许一张占用最后容量"
        );
        assert_eq!(
            store.create(template()).await.unwrap_err().status(),
            axum::http::StatusCode::CONFLICT
        );
        assert_eq!(store.list().await.unwrap().len(), MAX_TEMPLATES);
    }

    #[tokio::test]
    async fn refuses_storage_path_that_is_not_a_regular_file() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let path = directory.0.join("templates.sqlite3");
        std::fs::create_dir(&path).unwrap();
        assert_eq!(
            store.create(template()).await.unwrap_err().status(),
            axum::http::StatusCode::INTERNAL_SERVER_ERROR
        );
    }

    fn legacy_record(id: &str) -> Value {
        json!({"id":id,"revision":7,"created_at":1700000000000u64,"updated_at":1700000000123u64,"template":template()})
    }

    async fn pending_archive(directory: &TestDirectory, id: &str, record: &Value) -> Vec<u8> {
        let filename = format!("{id}.json");
        let bytes = serde_json::to_vec(record).unwrap();
        fs::write(directory.0.join(&filename), &bytes).unwrap();
        directory.store().initialize().await.unwrap();
        {
            let database =
                rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
            database
                .execute(
                    "UPDATE storage_meta SET value='pending' WHERE key='legacy_migration'",
                    [],
                )
                .unwrap();
            database
                .execute(
                    "INSERT INTO legacy_files VALUES (?1,?2)",
                    rusqlite::params![id, bytes],
                )
                .unwrap();
        }
        let archive = directory.0.join("legacy-json");
        if archive.exists() {
            fs::remove_file(archive.join(&filename)).unwrap();
            fs::remove_dir(&archive).unwrap();
        }
        fs::write(directory.0.join(&filename), &bytes).unwrap();
        let marker = directory.0.join("database-required.json");
        if marker.is_dir() {
            fs::remove_dir(marker).unwrap();
        } else {
            fs::remove_file(marker).unwrap();
        }
        bytes
    }

    #[tokio::test]
    async fn migrates_legacy_records_once_and_blocks_old_json_access() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        let original = serde_json::to_vec_pretty(&record).unwrap();
        let filename = format!("{id}.json");
        fs::write(directory.0.join(&filename), &original).unwrap();
        let store = directory.store();
        assert_eq!(store.get(&id).await.unwrap(), record);
        assert!(directory.0.join("templates.sqlite3").is_file());
        assert!(directory.0.join("database-required.json").is_dir());
        assert!(!directory.0.join(&filename).exists());
        assert!(!directory.0.join("legacy-json").exists());
        assert!(
            fs::read_dir(&directory.0).unwrap().all(|entry| {
                let path = entry.unwrap().path();
                !path.is_file() || path.extension().and_then(|value| value.to_str()) != Some("json")
            }),
            "迁移完成不能保留任何 JSON 业务或标记文件"
        );
        assert!(
            read_records(&directory.0).is_err(),
            "旧版本必须拒绝创建空 JSON 库"
        );
        store.delete(&id, 7).await.unwrap();
        assert!(matches!(
            directory.store().get(&id).await,
            Err(StoreError::NotFound)
        ));
    }

    #[tokio::test]
    async fn corrupt_legacy_record_rolls_back_entire_migration_then_can_retry() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        let filename = format!("{id}.json");
        let corrupt = directory.0.join(format!("{}.json", Uuid::new_v4()));
        fs::write(
            directory.0.join(&filename),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        fs::write(&corrupt, b"{broken").unwrap();
        assert!(directory.store().list().await.is_err());
        assert!(directory.0.join(&filename).is_file());
        assert_eq!(fs::read(&corrupt).unwrap(), b"{broken");
        let database = rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
        let tables: u32 = database
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='templates'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tables, 0, "损坏记录必须回滚整个迁移事务，包含表结构");
        drop(database);
        fs::remove_file(corrupt).unwrap();
        assert_eq!(directory.store().get(&id).await.unwrap(), record);
    }

    #[tokio::test]
    async fn interrupted_archive_refuses_service_and_retries_without_importing_twice() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        fs::write(
            directory.0.join(format!("{id}.json")),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        File::create(directory.0.join("templates.sqlite3")).unwrap();
        fs::write(directory.0.join("legacy-json"), "阻止归档").unwrap();
        assert!(directory.store().list().await.is_err());
        assert!(directory.0.join("database-required.json").is_dir());
        fs::remove_file(directory.0.join("legacy-json")).unwrap();
        let store = directory.store();
        assert_eq!(store.list().await.unwrap().len(), 1);
        assert_eq!(store.get(&id).await.unwrap(), record);
    }

    #[tokio::test]
    async fn summary_uses_metadata_index_without_reading_template_content() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let record = store.create(template()).await.unwrap();
        store.run(|database| {
            database.execute("UPDATE templates SET template_json='{broken'", []).unwrap();
            let plan: String = database.query_row("EXPLAIN QUERY PLAN SELECT id FROM templates ORDER BY updated_at DESC,id ASC", [], |row| row.get(3)).unwrap();
            assert!(plan.contains("templates_updated"), "摘要排序必须使用数据库索引");
            Ok(())
        }).await.unwrap();
        assert_eq!(store.list().await.unwrap()[0]["name"], "测试点表");
        assert!(store.get(record["id"].as_str().unwrap()).await.is_err());
    }

    #[tokio::test]
    async fn migration_preserves_full_unsigned_metadata_and_exact_decimal_text() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let mut record = legacy_record(&id);
        record["revision"] = json!(u64::MAX - 1);
        record["created_at"] = json!(u64::MAX - 1);
        record["updated_at"] = json!(u64::MAX);
        fs::write(
            directory.0.join(format!("{id}.json")),
            serde_json::to_vec(&record).unwrap(),
        )
        .unwrap();
        let store = directory.store();
        assert_eq!(store.get(&id).await.unwrap(), record);
        let updated = store.update(&id, u64::MAX - 1, template()).await.unwrap();
        assert_eq!(updated["revision"], u64::MAX);
        assert_eq!(updated["updated_at"], u64::MAX);
        assert!(matches!(
            store.update(&id, u64::MAX, template()).await,
            Err(StoreError::Storage(_))
        ));
        assert!(matches!(
            store.delete(&id, u64::MAX).await,
            Err(StoreError::Storage(_))
        ));
        assert_eq!(store.get(&id).await.unwrap(), updated);
    }

    #[tokio::test]
    async fn migration_commit_then_marker_failure_retries_without_losing_records() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        pending_archive(&directory, &id, &record).await;
        // 模拟标记落盘失败，数据库已提交后仍应保留待归档状态。
        fs::write(directory.0.join("database-required.json"), "阻止保护目录").unwrap();
        assert!(directory.store().initialize().await.is_err());
        fs::remove_file(directory.0.join("database-required.json")).unwrap();
        assert_eq!(directory.store().get(&id).await.unwrap(), record);
    }

    #[tokio::test]
    async fn pending_archive_rejects_legacy_edits_and_conflicting_backups_without_erasing_them() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        let original = pending_archive(&directory, &id, &record).await;
        let path = directory.0.join(format!("{id}.json"));
        let backup = directory.0.join("legacy-json").join(format!("{id}.json"));
        let mut changed = record.clone();
        changed["template"]["name"] = json!("旧程序迁移后更新");
        let bytes = serde_json::to_vec(&changed).unwrap();
        fs::write(&path, &bytes).unwrap();
        assert!(directory.store().initialize().await.is_err());
        assert_eq!(fs::read(&path).unwrap(), bytes);
        fs::write(&path, &original).unwrap();
        fs::create_dir_all(directory.0.join("legacy-json")).unwrap();
        fs::write(&backup, &bytes).unwrap();
        assert!(directory.store().initialize().await.is_err());
        assert_eq!(fs::read(&backup).unwrap(), bytes);
        assert_eq!(fs::read(&path).unwrap(), original);
        fs::remove_file(&backup).unwrap();
        assert_eq!(directory.store().get(&id).await.unwrap(), record);
    }

    #[tokio::test]
    async fn lost_database_cannot_reinitialize_empty_store_from_archived_json() {
        let directory = TestDirectory::new();
        directory.store().create(template()).await.unwrap();
        fs::remove_file(directory.0.join("templates.sqlite3")).unwrap();
        assert!(directory.store().initialize().await.is_err());
        assert!(!directory.0.join("templates.sqlite3").exists());
    }

    #[tokio::test]
    async fn cleanup_retries_when_original_and_backup_were_already_removed() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        pending_archive(&directory, &id, &record).await;
        fs::remove_file(directory.0.join(format!("{id}.json"))).unwrap();
        assert_eq!(directory.store().get(&id).await.unwrap(), record);
        assert!(!directory.0.join("legacy-json").exists());
    }

    #[tokio::test]
    async fn interrupted_marker_conversion_keeps_old_version_blocked_and_can_resume() {
        let directory = TestDirectory::new();
        let id = Uuid::new_v4().to_string();
        let record = legacy_record(&id);
        pending_archive(&directory, &id, &record).await;
        fs::write(
            directory.0.join("database-required-previous.json"),
            b"{\"storage\":\"sqlite\",\"schema_version\":1}\n",
        )
        .unwrap();
        assert!(
            read_records(&directory.0).is_err(),
            "标记转换中断后旧版仍须拒绝创建空库"
        );
        assert_eq!(directory.store().get(&id).await.unwrap(), record);
        assert!(directory.0.join("database-required.json").is_dir());
        assert!(!directory.0.join("database-required-previous.json").exists());
        assert!(!directory.0.join(format!("{id}.json")).exists());
    }

    fn old_marker_file(directory: &TestDirectory) {
        let marker = directory.0.join("database-required.json");
        if marker.is_dir() {
            fs::remove_dir(&marker).unwrap();
        }
        fs::write(marker, b"{\"storage\":\"sqlite\",\"schema_version\":1}\n").unwrap();
    }

    #[tokio::test]
    async fn cleans_previous_complete_migration_without_reviving_deleted_or_old_records() {
        let directory = TestDirectory::new();
        let store = directory.store();
        let live = store.create(template()).await.unwrap();
        let deleted = store.create(template()).await.unwrap();
        let live_id = live["id"].as_str().unwrap();
        let deleted_id = deleted["id"].as_str().unwrap();
        let mut updated_template = template();
        updated_template["name"] = json!("已更新的数据库点表");
        let updated = store.update(live_id, 1, updated_template).await.unwrap();
        store.delete(deleted_id, 1).await.unwrap();
        old_marker_file(&directory);
        let archive = directory.0.join("legacy-json");
        fs::create_dir(&archive).unwrap();
        for record in [live.clone(), deleted.clone()] {
            fs::write(
                archive.join(format!("{}.json", record["id"].as_str().unwrap())),
                serde_json::to_vec(&record).unwrap(),
            )
            .unwrap();
        }
        let reopened = directory.store();
        reopened.initialize().await.unwrap();
        assert_eq!(reopened.get(live_id).await.unwrap(), updated);
        assert!(matches!(
            reopened.get(deleted_id).await,
            Err(StoreError::NotFound)
        ));
        assert!(!archive.exists());
        assert!(directory.0.join("database-required.json").is_dir());
        assert!(reopened.list().await.unwrap().len() == 1);
    }

    #[tokio::test]
    async fn refuses_archive_with_unrelated_files_without_deleting_them() {
        let directory = TestDirectory::new();
        directory.store().initialize().await.unwrap();
        old_marker_file(&directory);
        let archive = directory.0.join("legacy-json");
        fs::create_dir(&archive).unwrap();
        let unrelated = archive.join("用户资料.txt");
        fs::write(&unrelated, "不要删除").unwrap();
        assert!(directory.store().initialize().await.is_err());
        assert_eq!(fs::read_to_string(unrelated).unwrap(), "不要删除");
    }

    #[tokio::test]
    async fn missing_database_table_is_rejected_without_recreating_empty_library() {
        let directory = TestDirectory::new();
        directory.store().create(template()).await.unwrap();
        {
            let database =
                rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
            database.execute("DROP TABLE templates", []).unwrap();
        }
        assert!(directory.store().initialize().await.is_err());
        let database = rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
        let tables: u32 = database
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='templates'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(tables, 0, "不能把缺表的数据损坏掩盖成空库");
    }

    #[tokio::test]
    async fn rejects_unknown_database_version_and_missing_or_corrupt_marker() {
        let directory = TestDirectory::new();
        directory.store().initialize().await.unwrap();
        let marker = directory.0.join("database-required.json");
        if marker.is_dir() {
            fs::remove_dir(&marker).unwrap();
        } else {
            fs::remove_file(&marker).unwrap();
        }
        assert!(directory.store().initialize().await.is_err());
        fs::write(&marker, "损坏标记").unwrap();
        assert!(directory.store().initialize().await.is_err());
        fs::remove_file(&marker).unwrap();
        fs::create_dir(&marker).unwrap();
        {
            let database =
                rusqlite::Connection::open(directory.0.join("templates.sqlite3")).unwrap();
            database.pragma_update(None, "user_version", 999).unwrap();
        }
        assert!(directory.store().initialize().await.is_err());
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
