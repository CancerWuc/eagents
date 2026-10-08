<script setup>
// 登录页（admin-auth 01）：Pluto 员工登录跳转 + admin 本地表单（跟随开关隐藏）
import { onMounted, ref } from 'vue'
import {
  consoleCallback,
  fetchAuthProviders,
  fetchConsoleLogin,
  loginAdmin,
} from '../auth.js'

const emit = defineEmits(['logged-in'])
const localEnabled = ref(true)
const localFormReady = ref(false) // 开关接口可达后再显示表单，避免闪烁
const plutoReady = ref(true)
const plutoError = ref('')
const username = ref('admin')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function plutoLogin() {
  error.value = ''
  busy.value = true
  try {
    const { authorize_url } = await fetchConsoleLogin()
    window.location.href = authorize_url
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}


async function localLogin() {
  error.value = ''
  busy.value = true
  try {
    await loginAdmin(username.value.trim(), password.value)
    emit('logged-in')
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  // Pluto 回跳：?code=xx&state=xx（redirect_uri 指向本页）
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const state = params.get('state')
  if (code && state) {
    busy.value = true
    try {
      await consoleCallback(code, state)
      window.history.replaceState({}, '', window.location.pathname)
      emit('logged-in')
      return
    } catch (e) {
      error.value = `Pluto 登录失败：${e.message}`
      window.history.replaceState({}, '', window.location.pathname)
    } finally {
      busy.value = false
    }
  }
  try {
    const { pluto, local_login } = await fetchAuthProviders()
    plutoReady.value = pluto
    if (!pluto) plutoError.value = 'Pluto 未配置，员工登录不可用（请使用本地管理员账号登录）'
    localEnabled.value = local_login
    localFormReady.value = true
  } catch {
    localFormReady.value = true
  }
})
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-canvas px-4">
    <div class="card w-full max-w-md">
      <h1 class="text-lg font-semibold mb-1">eAgent 管理后台登录</h1>
      <p class="text-t3 text-xs mb-5">员工请使用 Pluto 账号登录；管理员可走本地账号</p>

      <div v-if="error" class="badge-err badge w-full justify-center mb-4 px-3 py-2">{{ error }}</div>

      <button class="btn btn-primary w-full justify-center mb-3" :disabled="busy || !plutoReady" @click="plutoLogin">
        使用 Pluto 员工账号登录
      </button>
      <p v-if="!plutoReady" class="text-warn text-xs mb-3">{{ plutoError }}</p>

      <template v-if="localEnabled && localFormReady">
        <div class="flex items-center gap-3 my-4">
          <div class="flex-1 border-t border-line"></div>
          <span class="text-t3 text-xs">或本地管理员登录</span>
          <div class="flex-1 border-t border-line"></div>
        </div>
        <form class="flex flex-col gap-3" @submit.prevent="localLogin">
          <input v-model="username" class="input" placeholder="用户名" autocomplete="username" />
          <input v-model="password" class="input" type="password" placeholder="密码" autocomplete="current-password" />
          <button class="btn btn-primary w-full justify-center" type="submit" :disabled="busy || !password">
            登录
          </button>
        </form>
      </template>
      <p v-else-if="localFormReady" class="text-t3 text-xs text-center">
        本地登录已关闭，请使用员工账号（Pluto）登录
      </p>
    </div>
  </div>
</template>
