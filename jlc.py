import os
import sys
import time
import json
import tempfile
import random
import requests
from datetime import datetime
from urllib.parse import urlsplit, urlencode
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

SIGN_IN_URL = "https://oshwhub.com/sign_in"
LOGIN_URL = "https://passport.jlc.com/login?" + urlencode({
    "appId": "JLC_OSHWHUB",
    "redirectUrl": SIGN_IN_URL,
    "backCode": "1",
})


def is_passport_login(url):
    page = urlsplit(url)
    return page.hostname == "passport.jlc.com" and page.path.rstrip("/") == "/login"


def is_sign_in_page(url):
    page = urlsplit(url)
    return page.hostname == "oshwhub.com" and page.path.rstrip("/") == "/sign_in"


def prepare_login(driver, account_index):
    """主动进入网站提供的 SSO 地址，不依赖签到页自动跳转。"""
    driver.get(LOGIN_URL)
    WebDriverWait(driver, 30).until(
        lambda d: is_passport_login(d.current_url) or is_sign_in_page(d.current_url)
    )
    log(f"账号 {account_index} - 已进入统一登录流程")
    return is_passport_login(driver.current_url)


def get_login_blocker(driver):
    """只返回脱敏后的登录问题类别，不在日志中输出网页原文。"""
    error_selectors = (
        ".el-message--error .el-message__content",
        ".el-form-item__error",
        "[role='alert']",
        ".error-message",
    )
    for selector in error_selectors:
        try:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if not element.is_displayed():
                    continue
                message = element.text.strip()
                if not message:
                    continue
                if any(word in message for word in ("验证", "滑块", "验证码", "安全")):
                    return "网站提示需要完成安全验证，GitHub Actions 无法代替人工验证"
                if any(word in message for word in ("密码", "账号", "账户", "客户编号")):
                    return "网站提示账号或密码有问题，请检查 GitHub Secrets 和官网登录是否正常"
                if any(word in message for word in ("频繁", "限制", "异常", "网络")):
                    return "网站提示请求受限或网络异常，请稍后在官网确认"
                if selector != "[role='alert']":
                    return "网站出现登录错误提示（内容已脱敏，未记录账号或密码）"
        except Exception:
            continue

    # 检测验证码/安全验证控件，不自动尝试破解或模拟人工验证。
    challenge_selectors = (
        ".btn_slide", ".nc_scale", "iframe[src*='captcha']",
        "iframe[src*='verify']", "[class*='captcha']",
    )
    for selector in challenge_selectors:
        try:
            if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, selector)):
                return "登录页面出现安全验证控件，需要本人在官方网站完成验证"
        except Exception:
            continue
    return None


def log_login_http_errors(driver, account_index):
    """仅输出登录站点的 HTTP 错误状态码，不泄露请求体、Cookie 或 URL 参数。"""
    try:
        statuses = set()
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"]).get("message", {})
            if message.get("method") != "Network.responseReceived":
                continue
            response = message.get("params", {}).get("response", {})
            if urlsplit(response.get("url", "")).hostname != "passport.jlc.com":
                continue
            status = int(response.get("status", 0))
            if status >= 400:
                statuses.add(status)
        if statuses:
            log(f"账号 {account_index} - 登录中心 HTTP 错误状态码: {', '.join(map(str, sorted(statuses)))}")
    except Exception:
        pass


def wait_for_login_result(driver, account_index, timeout=40):
    """区分登录成功、网站报错和人机验证，不再盲等旧版滑块控件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_sign_in_page(driver.current_url):
            log(f"账号 {account_index} - ✅ 登录成功，已返回开源平台签到页")
            return True

        blocker = get_login_blocker(driver)
        if blocker:
            log(f"账号 {account_index} - ❌ {blocker}")
            log_login_http_errors(driver, account_index)
            return False
        time.sleep(1)

    page = urlsplit(driver.current_url)
    log(f"账号 {account_index} - ❌ 登录 40 秒内未完成，当前页面: {page.netloc}{page.path}")
    log(f"账号 {account_index} - 可能是验证未完成、账号信息错误或站点响应异常")
    log_login_http_errors(driver, account_index)
    return False


def sign_in_oshwhub(driver, account_index):
    """只有页面确认已签到才成功；单纯点击不代表服务器处理成功。"""
    signed = (By.XPATH, '//span[normalize-space(.)="已签到"]')
    pending = (By.XPATH, '//span[contains(text(),"立即签到")]')
    wait = WebDriverWait(driver, 30)
    if any(e.is_displayed() for e in driver.find_elements(*signed)):
        log(f"账号 {account_index} - ✅ 今天已经在开源平台签到过了")
        return True
    wait.until(EC.element_to_be_clickable(pending)).click()
    wait.until(EC.visibility_of_element_located(signed))
    log(f"账号 {account_index} - ✅ 开源平台已确认签到成功")
    return True

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def extract_token_from_local_storage(driver):
    """直接从 localStorage 提取 X-JLC-AccessToken"""
    try:
        token = driver.execute_script("return window.localStorage.getItem('X-JLC-AccessToken');")
        if token:
            log("✅ 成功从 localStorage 提取访问凭证")
            return token
        else:
            log("❌ localStorage 中未找到 X-JLC-AccessToken")
            # 尝试其他可能的键名
            alternative_keys = [
                "x-jlc-accesstoken",
                "accessToken", 
                "token",
                "jlc-token"
            ]
            for key in alternative_keys:
                token = driver.execute_script(f"return window.localStorage.getItem('{key}');")
                if token:
                    log(f"✅ 从 localStorage 的 {key} 提取到访问凭证")
                    return token
    except Exception as e:
        log(f"❌ 从 localStorage 提取 token 失败: {e}")
    
    return None

def extract_secretkey_from_devtools(driver):
    """使用 DevTools 从网络请求中提取 secretkey"""
    secretkey = None
    
    try:
        # 获取性能日志
        logs = driver.get_log('performance')
        
        for entry in logs:
            try:
                message = json.loads(entry['message'])
                message_type = message.get('message', {}).get('method', '')
                
                # 检查网络请求
                if message_type == 'Network.requestWillBeSent':
                    request = message.get('message', {}).get('params', {}).get('request', {})
                    url = request.get('url', '')
                    
                    # 检查所有 m.jlc.com 的请求
                    if 'm.jlc.com' in url:
                        headers = request.get('headers', {})
                        # 尝试多种可能的键名
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log("✅ 从网络请求中提取到签名凭证")
                            break
                
                # 检查网络响应
                elif message_type == 'Network.responseReceived':
                    response = message.get('message', {}).get('params', {}).get('response', {})
                    url = response.get('url', '')
                    
                    if 'm.jlc.com' in url:
                        headers = response.get('requestHeaders', {})
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log("✅ 从网络响应中提取到签名凭证")
                            break
                            
            except Exception as e:
                continue
                
    except Exception as e:
        log(f"❌ DevTools 提取 secretkey 出错: {e}")
    
    return secretkey

class JLCClient:
    """嘉立创 API 客户端"""
    
    def __init__(self, access_token, secretkey, account_index):
        self.base_url = "https://m.jlc.com"
        self.headers = {
            'user-agent': self.get_random_user_agent(),
            'x-jlc-clienttype': 'WEB',
            'accept': 'application/json, text/plain, */*',
            'x-jlc-accesstoken': access_token,
            'secretkey': secretkey,
            'Referer': 'https://m.jlc.com/mapp/pages/my/index',
        }
        self.account_index = account_index
        self.message = ""
        
    def get_random_user_agent(self):
        """获取随机 User-Agent"""
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        return random.choice(user_agents)
    
    def send_request(self, url, method='GET', data=None):
        """发送 API 请求"""
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, timeout=10)
            else:
                response = requests.post(url, headers=self.headers, json=data, timeout=10)
            
            return response.json()
        except Exception as e:
            log(f"账号 {self.account_index} - ❌ 请求失败 ({url}): {e}")
            return None
    
    def get_user_info(self):
        """获取用户信息"""
        log(f"账号 {self.account_index} - 获取用户信息...")
        url = f"{self.base_url}/api/appPlatform/center/setting/selectPersonalInfo"
        data = self.send_request(url)
        
        if data and data.get('success'):
            customer_code = data.get('data', {}).get('customerCode', '未知')
            log(f"账号 {self.account_index} - 用户编号: {customer_code}")
            self.message += f"用户编号: {customer_code}\n"
            return True
        else:
            log(f"账号 {self.account_index} - ❌ 获取用户信息失败: {data}")
            return False
    
    def check_sign_status(self):
        """检查签到状态"""
        log(f"账号 {self.account_index} - 检查签到状态...")
        url = f"{self.base_url}/api/activity/sign/getCurrentUserSignInConfig"
        data = self.send_request(url)
        
        if data and data.get('success'):
            have_sign_in = data.get('data', {}).get('haveSignIn', False)
            if have_sign_in:
                log(f"账号 {self.account_index} - ✅ 今日已签到")
                self.message += "今日已签到\n"
                return True
            else:
                log(f"账号 {self.account_index} - 今日未签到")
                return False
        else:
            log(f"账号 {self.account_index} - ❌ 检查签到状态失败: {data}")
            return None
    
    def sign_in(self):
        """执行签到"""
        log(f"账号 {self.account_index} - 执行签到...")
        url = f"{self.base_url}/api/activity/sign/signIn?source=4"
        data = self.send_request(url)
        
        if data and data.get('success'):
            gain_num = data.get('data', {}).get('gainNum')
            if gain_num:
                log(f"账号 {self.account_index} - ✅ 签到成功，金豆+{gain_num}")
                self.message += f"签到成功，金豆+{gain_num}\n"
                return True
            else:
                log(f"账号 {self.account_index} - 有奖励可领取，先领取奖励")
                return self.receive_voucher()
        else:
            log(f"账号 {self.account_index} - ❌ 签到失败: {data}")
            return False
    
    def receive_voucher(self):
        """领取奖励"""
        log(f"账号 {self.account_index} - 领取奖励...")
        url = f"{self.base_url}/api/activity/sign/receiveVoucher"
        data = self.send_request(url)
        
        if data and data.get('success'):
            log(f"账号 {self.account_index} - ✅ 领取成功")
            self.message += "领取成功\n"
            # 领取奖励后再次签到
            time.sleep(random.randint(1, 2))
            return self.sign_in()
        else:
            log(f"账号 {self.account_index} - ❌ 领取奖励失败: {data}")
            return False
    
    def get_points(self):
        """获取金豆数量"""
        log(f"账号 {self.account_index} - 获取金豆数量...")
        url = f"{self.base_url}/api/activity/front/getCustomerIntegral"
        data = self.send_request(url)
        
        if data and data.get('success'):
            integral_voucher = data.get('data', {}).get('integralVoucher', 0)
            log(f"账号 {self.account_index} - 当前金豆: {integral_voucher}")
            self.message += f"当前金豆: {integral_voucher}\n\n"
            return integral_voucher
        else:
            log(f"账号 {self.account_index} - ❌ 获取金豆数量失败: {data}")
            return 0
    
    def execute_full_process(self):
        """执行完整的金豆签到流程"""
        log(f"账号 {self.account_index} - 开始完整金豆签到流程")
        
        # 1. 获取用户信息
        if not self.get_user_info():
            return False
        
        time.sleep(random.randint(1, 2))
        
        # 2. 检查签到状态
        sign_status = self.check_sign_status()
        if sign_status is None:
            return False
        
        if not sign_status:
            # 3. 执行签到
            time.sleep(random.randint(2, 3))
            if not self.sign_in():
                return False
        
        time.sleep(random.randint(1, 2))
        
        # 4. 获取金豆数量
        self.get_points()
        
        return True

def navigate_and_interact_m_jlc(driver, account_index):
    """在 m.jlc.com 进行导航和交互以触发网络请求"""
    log(f"账号 {account_index} - 在 m.jlc.com 进行交互操作...")
    
    try:
        # 等待页面加载
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        
        # 滚动页面
        driver.execute_script("window.scrollTo(0, 300);")
        time.sleep(2)
        
        # 尝试点击可能的导航元素
        nav_selectors = [
            "//div[contains(text(), '我的')]",
            "//div[contains(text(), '个人中心')]",
            "//div[contains(text(), '用户中心')]",
            "//a[contains(@href, 'user')]",
            "//a[contains(@href, 'center')]",
            "//div[@class='tabbar']//div[contains(text(), '我的')]",
            "//div[@class='footer']//div[contains(text(), '我的')]"
        ]
        
        for selector in nav_selectors:
            try:
                element = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                element.click()
                log(f"账号 {account_index} - 点击导航元素: {selector}")
                time.sleep(3)
                break
            except:
                continue
        
        # 再次滚动
        driver.execute_script("window.scrollTo(0, 500);")
        time.sleep(2)
        
        # 尝试刷新页面
        driver.refresh()
        time.sleep(5)
        
    except Exception as e:
        log(f"账号 {account_index} - 交互操作出错: {e}")

def sign_in_account(username, password, account_index, total_accounts):
    """为单个账号执行完整的签到流程"""
    log(f"开始处理账号 {account_index}/{total_accounts}")
    
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    # 启用性能日志
    caps = DesiredCapabilities.CHROME
    caps['goog:loggingPrefs'] = {'performance': 'ALL'}
    
    driver = webdriver.Chrome(options=chrome_options, desired_capabilities=caps)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    wait = WebDriverWait(driver, 25)
    oshwhub_success = False
    jindou_success = False
    jlc_message = ""

    try:
        # 1. 使用当前网页提供的 SSO 参数主动进入登录中心。
        # 已有 SSO 会话可以直接回到签到页。
        if prepare_login(driver, account_index):
            log(f"账号 {account_index} - 检测到未登录状态，正在执行登录流程...")

            try:
                # 切换到账号登录
                phone_btn = wait.until(
                    EC.element_to_be_clickable((By.XPATH, '//button[contains(text(),"账号登录")]'))
                )
                phone_btn.click()
                log(f"账号 {account_index} - 已切换账号登录")
                time.sleep(3)
            except Exception as e:
                log(f"账号 {account_index} - 账号登录按钮可能已默认选中: {e}")

            # 输入账号密码
            try:
                user_input = wait.until(
                    EC.presence_of_element_located((By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]'))
                )
                user_input.clear()
                user_input.send_keys(username)

                pwd_input = wait.until(
                    EC.presence_of_element_located((By.XPATH, '//input[@type="password"]'))
                )
                pwd_input.clear()
                pwd_input.send_keys(password)
                log(f"账号 {account_index} - 已输入账号密码")
            except Exception as e:
                log(f"账号 {account_index} - ❌ 登录输入框未找到: {e}")
                return False

            # 点击登录
            try:
                login_btn = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "button.submit"))
                )
                login_btn.click()
                log(f"账号 {account_index} - 已点击登录按钮")
            except Exception as e:
                log(f"账号 {account_index} - ❌ 登录按钮定位失败: {e}")
                return False

            # 登录按钮提交后，旧版 .btn_slide 并不一定存在。
            # 实际检查跳转、网站错误提示及安全验证，避免空等和误报。
            log(f"账号 {account_index} - 正在确认登录结果...")
            if not wait_for_login_result(driver, account_index):
                return False

        # 3. 开源平台签到
        log(f"账号 {account_index} - 等待签到页加载...")
        time.sleep(5)

        try:
            driver.refresh()
            time.sleep(4)
        except:
            pass

        # 执行开源平台签到
        oshwhub_success = False
        try:
            oshwhub_success = sign_in_oshwhub(driver, account_index)
        except Exception as e:
            log(f"账号 {account_index} - ⚠ 开源平台签到按钮: {e}")
            log(f"账号 {account_index} - ❌ 开源平台未确认签到成功")

        time.sleep(3)

        # 4. 金豆签到流程
        log(f"账号 {account_index} - 开始金豆签到流程...")
        
        # 移动端根路径现在可能跳转到 www.jlc.com；使用可直接访问的个人中心。
        driver.get("https://m.jlc.com/mapp/pages/my/index")
        page = urlsplit(driver.current_url)
        log(f"账号 {account_index} - 已访问移动端页面: {page.netloc}{page.path}")
        
        # 延长等待时间
        time.sleep(15)
        
        # 进行交互操作以触发网络请求
        navigate_and_interact_m_jlc(driver, account_index)
        
        # 提取 token (从 localStorage)
        access_token = extract_token_from_local_storage(driver)
        
        # 提取 secretkey (从 DevTools)
        secretkey = extract_secretkey_from_devtools(driver)
        
        if access_token and secretkey:
            log(f"账号 {account_index} - ✅ 成功提取 token 和 secretkey")
            
            # 创建 JLC 客户端并执行完整流程
            jlc_client = JLCClient(access_token, secretkey, account_index)
            jindou_success = jlc_client.execute_full_process()
            jlc_message = jlc_client.message
            
            if jindou_success:
                log(f"账号 {account_index} - ✅ 金豆签到流程完成")
            else:
                log(f"账号 {account_index} - ❌ 金豆签到流程失败")
        else:
            log(f"账号 {account_index} - ❌ 无法提取到 token 或 secretkey，跳过金豆签到")
            if not access_token:
                log(f"账号 {account_index} - ❌ Token 提取失败")
            if not secretkey:
                log(f"账号 {account_index} - ❌ SecretKey 提取失败")
            
            page = urlsplit(driver.current_url)
            log(f"账号 {account_index} - 页面位置: {page.netloc}{page.path}（日志不输出本地存储内容）")

    except Exception as e:
        log(f"账号 {account_index} - ❌ 程序执行错误: {e}")
        try:
            driver.save_screenshot(f"error_screenshot_account_{account_index}.png")
        except:
            pass

    finally:
        driver.quit()
        log(f"账号 {account_index} - 浏览器已关闭")
    
    # 输出账号处理结果
    if jlc_message:
        log(f"账号 {account_index} - 金豆签到结果:\n{jlc_message}")
    
    # 开源平台和金豆必须分别成功，才算本账号完整签到成功。
    return oshwhub_success and jindou_success

def main():
    # Actions 通过环境变量传入凭证，避免密码出现在进程命令行和执行日志中。
    # 保留本地命令行运行的兼容性。
    if len(sys.argv) >= 3:
        username_list, password_list = sys.argv[1:3]
    else:
        username_list = os.environ.get("JLC_USERNAME", "")
        password_list = os.environ.get("JLC_PASSWORD", "")

    usernames = [u.strip() for u in username_list.split(',') if u.strip()]
    passwords = [p.strip() for p in password_list.split(',') if p.strip()]

    if not usernames or not passwords:
        log("❌ 未配置嘉立创账号或密码。请检查 JLC_USERNAME / JLC_PASSWORD Secrets")
        sys.exit(1)
    
    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)
    
    total_accounts = len(usernames)
    log(f"开始处理 {total_accounts} 个账号的签到任务")

    success_count = 0
    failed_accounts = []

    for i, (username, password) in enumerate(zip(usernames, passwords), 1):
        log(f"开始处理第 {i} 个账号")

        success = sign_in_account(username, password, i, total_accounts)

        if success:
            success_count += 1
            log(f"✅ 第 {i} 个账号签到成功")
        else:
            failed_accounts.append(i)
            log(f"❌ 第 {i} 个账号签到失败")

        if i < total_accounts:
            wait_time = random.randint(15, 40)
            log(f"等待 {wait_time} 秒后处理下一个账号...")
            time.sleep(wait_time)

    log("=" * 50)
    log("签到任务完成总结:")
    log(f"总账号数: {total_accounts}")
    log(f"成功数: {success_count}")
    log(f"失败数: {len(failed_accounts)}")

    if failed_accounts:
        log(f"失败的账号序号: {', '.join(map(str, failed_accounts))}")
    else:
        log("✅ 所有账号签到成功!")

    log("=" * 50)

    if failed_accounts:
        sys.exit(1)


if __name__ == "__main__":
    main()
