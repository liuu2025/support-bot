# 本地私有配置

此目录只用于保存本机开发、评测和部署所需的环境变量，不提交真实密钥、客户数据、数据库文件或日志。

创建 `support-bot.env` 后，可使用：

```powershell
docker compose --env-file private/support-bot.env -p support-bot up -d --build
```

建议至少设置随机生成的：

```dotenv
JWT_SECRET=本机生成的至少32字节随机值
CONFIG_SECRET_KEY=本机生成的Fernet密钥
POSTGRES_PASSWORD=本机生成的随机值
DATABASE_URL=postgresql://support_bot:请进行URL编码的密码@postgres:5432/support_bot
```

真实 API Key 只在本机环境变量或网页模型设置中使用。公开仓库只保留 `.env.example` 这类不含凭据的模板。
