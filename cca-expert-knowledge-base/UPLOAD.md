# 上传到 GitHub

本文档说明如何把本仓库上传到 GitHub，以及之后如何同步更新。

> 当前机器没有配置 Git 身份、SSH key、`gh` CLI 或已保存的 token。
> 下面的命令在**宿主机**执行即可；Git 只写 `.git` 元数据，不会安装依赖或污染环境。

---

## 0. 前提

- 已安装 Git：`git --version`
- 能访问 GitHub：`curl -I https://github.com`
- 已在 GitHub 注册账号

---

## 1. 在 GitHub 上创建仓库

1. 打开 <https://github.com/new>
2. 填写 **Repository name**，例如 `cca-expert-knowledge-base`
3. 选择 Public 或 Private
4. **不要**勾选以下选项，否则会和本地首次提交冲突：
   - Add a README file
   - Add .gitignore
   - Choose a license
5. 点击 **Create repository**
6. 记下仓库地址，例如：

```text
https://github.com/<你的用户名>/cca-expert-knowledge-base.git
```

---

## 2. 配置 Git 身份并创建首次提交

```bash
cd /home/cll/cca-expert-knowledge-base

# 设置提交者身份（只需一次）
git config --global user.name "你的名字"
git config --global user.email "你的GitHub邮箱"

# 初始化仓库
git init

# 添加所有文件
git add .

# 提交前检查：确认没有密码、token、私钥或大文件
git status

# 创建首次提交
git commit -m "Initial CCA design-experience knowledge base"

# 统一主分支名
git branch -M main
```

如果不想公开真实邮箱，可以使用 GitHub 的 noreply 地址：

```bash
git config --global user.email "<你的用户名>@users.noreply.github.com"
```

---

## 3. 添加远程仓库

```bash
git remote add origin https://github.com/<你的用户名>/cca-expert-knowledge-base.git
```

检查：

```bash
git remote -v
```

---

## 4. 认证并推送（二选一）

### 方式 A：HTTPS + Personal Access Token（推荐，配置最快）

GitHub 已不再接受账号密码作为 Git 密码，必须使用 token。

1. 打开 <https://github.com/settings/tokens>
2. 点击 **Generate new token (classic)**
3. 勾选 `repo` 权限
4. 生成后复制 token（只显示一次，请妥善保存）
5. 推送：

```bash
git push -u origin main
```

输入提示：

- `Username`：GitHub 用户名
- `Password`：**粘贴 token**，不是账号密码

可选：让 Git 记住凭据，避免每次输入：

```bash
git config --global credential.helper store
```

> 注意：token 会明文保存在 `~/.git-credentials`。请确保该文件权限安全，
> 不要在共享机器上使用这种方式。

### 方式 B：SSH key

```bash
# 1. 生成密钥（一路回车，使用默认路径）
ssh-keygen -t ed25519 -C "你的GitHub邮箱"

# 2. 查看公钥
cat ~/.ssh/id_ed25519.pub
```

3. 把输出的整行内容添加到 <https://github.com/settings/keys>
   → **New SSH key**
4. 测试认证：

```bash
ssh -T git@github.com
# 看到 "Hi <用户名>!" 即成功
```

5. 把远程地址切换为 SSH 并推送：

```bash
git remote set-url origin git@github.com:<你的用户名>/cca-expert-knowledge-base.git
git push -u origin main
```

---

## 5. 确认上传结果

```bash
git log --oneline -1
git remote -v
```

浏览器刷新 GitHub 仓库页面，应能看到：

```text
knowledge/      结构化知识 YAML（唯一事实源）
skill/          零依赖检索 skill
context_pack/   静态提示词知识包
sources/        来源元数据与定位
src/            完整开发栈
tests/          回归测试
docker/         可选开发容器
```

---

## 6. 之后如何更新

修改 `knowledge/` 下的 YAML 后：

```bash
# 1. 重新生成 skill 快照与静态知识包
python3 tools/export_skill.py

# 2. 可选：校验与测试
pytest -q

# 3. 提交并推送
git add .
git commit -m "Update knowledge base"
git push
```

> `skill/knowledge.json` 和 `context_pack/` 虽然是生成物，但**需要提交**。
> 这样别人 clone 后无需安装依赖就能直接检索。`.gitignore` 已排除
> `__pycache__`、`.pytest_cache`、虚拟环境等缓存。

---

## 7. 常见问题

### `Permission denied (publickey)`

SSH key 没有添加到 GitHub，或 `ssh-agent` 中没有加载。按方式 B 重新添加公钥，
或用方式 A 的 HTTPS + token。

### `remote origin already exists`

```bash
git remote remove origin
git remote add origin <正确的仓库地址>
```

### `Updates were rejected because the remote contains work...`

说明远程仓库不是空的（可能创建时勾选了 README）。先拉取合并：

```bash
git pull --rebase origin main
git push -u origin main
```

或删除远程仓库后重新创建空仓库。

### 误把 token 或私钥提交了

1. 立即在 GitHub 撤销该 token / 删除该 SSH key；
2. 从历史中移除敏感文件（必要时使用 `git filter-repo`）；
3. 重新生成凭据。

### 想确认哪些文件会被提交

```bash
git status --short
git ls-files
```

---

## 8. 一次性命令清单

```bash
cd /home/cll/cca-expert-knowledge-base

git config --global user.name "你的名字"
git config --global user.email "你的GitHub邮箱"

git init
git add .
git status
git commit -m "Initial CCA design-experience knowledge base"
git branch -M main
git remote add origin https://github.com/<你的用户名>/cca-expert-knowledge-base.git
git push -u origin main
```

推送时输入 GitHub 用户名，密码处粘贴 Personal Access Token。
