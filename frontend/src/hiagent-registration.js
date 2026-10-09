// An unchanged enum can contain non-string values or be empty; retain it exactly.
function parameterEnum(row, requireMatchingType = false) {
  if ((!requireMatchingType || row.originalProperty?.type === row.type)
    && row.options === row.originalOptions && row.originalProperty?.enum) return row.originalProperty.enum
  const options = [...new Set((row.options || '').split(/[,，\n]/).map(value => value.trim()).filter(Boolean))]
  return options.length ? options : undefined
}

export function parameterChoices(row) { return parameterEnum(row) || [] }

// Convert the registration form into the existing backend contract.
export function buildHiagentRegistration(rows, query, originalSchema) {
  const properties = Object.create(null)
  const values = Object.create(null)
  const required = []
  const errors = {}
  const protectedKeys = new Set([
    'appkey', 'apikey', 'accesstoken', 'refreshtoken', 'token', 'password',
    'secret', 'authorization', 'capusername', 'realname', 'userid',
    'appconversationid',
  ])
  for (const [index, row] of rows.entries()) {
    const name = row.name.trim()
    const fail = message => { errors[index] = message }
    if (!name) { fail('请填写参数名'); continue }
    if (Object.hasOwn(properties, name)) { fail('参数名重复，请修改'); continue }
    if (protectedKeys.has(name.replace(/[_-]/g, '').toLowerCase())) {
      fail('凭证和系统身份字段不能放入 Inputs'); continue
    }
    if (!['string', 'number', 'integer', 'boolean', 'object', 'array'].includes(row.type)) {
      fail('请选择参数类型'); continue
    }
    const property = { ...(row.originalProperty?.type === row.type ? row.originalProperty : {}), type: row.type }
    delete property.description
    if (row.description.trim()) property.description = row.description.trim()
    if (row.type === 'string') {
      delete property.enum
      const options = parameterEnum(row, true)
      if (options) property.enum = options
    }
    properties[name] = property
    if (row.required) required.push(name)
    const raw = String(row.testValue ?? '')
    if (!raw.trim()) {
      if (row.required) fail('请填写必填参数的测试值')
      continue
    }
    let value = raw
    if (row.type === 'boolean') {
      if (!['true', 'false'].includes(raw)) { fail('请选择是或否'); continue }
      value = raw === 'true'
    } else if (row.type === 'number' || row.type === 'integer') {
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(raw.trim())) {
        fail('请填写有效数字'); continue
      }
      value = Number(raw)
      if (!Number.isFinite(value) || (row.type === 'integer' && !Number.isSafeInteger(value))) {
        fail(row.type === 'integer' ? '请填写有效整数' : '请填写有效数字'); continue
      }
    } else if (row.type === 'object' || row.type === 'array') {
      try { value = JSON.parse(raw) } catch { fail('请填写有效 JSON'); continue }
      if (row.type === 'array' ? !Array.isArray(value) : !value || typeof value !== 'object' || Array.isArray(value)) {
        fail(row.type === 'array' ? '请填写 JSON 数组' : '请填写 JSON 对象'); continue
      }
    } else if (property.enum?.length && !property.enum.includes(raw)) {
      fail('测试值必须是上面的可选值之一'); continue
    }
    values[name] = value
  }
  const schema = { ...(originalSchema || {}), type: 'object', properties, additionalProperties: originalSchema?.additionalProperties ?? false }
  delete schema.required
  if (required.length) schema.required = required
  return {
    schema,
    connectionTest: { query: query.trim(), inputs: values },
    errors,
    queryError: query.trim() ? '' : '请填写用于测试连接的问题',
  }
}

export function restoreHiagentRegistration(config, uuid) {
  const schema = config.inputsSchema || { type: 'object', properties: {}, additionalProperties: false }
  const test = config.connectionTest || { query: '你好', inputs: {} }
  return {
    schema,
    query: test.query,
    rows: Object.entries(schema.properties || {}).map(([name, property]) => {
      const value = test.inputs?.[name]
      const options = (property.enum || []).join('\n')
      return { id: uuid(), name, type: property.type, required: (schema.required || []).includes(name),
        description: property.description || '', options, originalOptions: options, originalProperty: property,
        testValue: value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value) }
    }),
  }
}
