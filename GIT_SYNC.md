# Git 双机同步说明

本项目通过 GitHub 仓库在两台机器之间同步：

```text
https://github.com/WANGZQ0221/AutoReview.git
```

## 第一次拉取项目

在另一台机器上执行：

```powershell
cd D:\
git clone https://github.com/WANGZQ0221/AutoReview.git
cd AutoReview
```

如果已经克隆过，就不需要再次 `git clone`。

## 日常同步流程

每次开始写代码前，先拉取远程最新内容：

```powershell
git pull
```

修改完成后，在当前机器提交并推送：

```powershell
git status
git add .
git commit -m "说明这次修改"
git push
```

另一台机器继续工作前，再执行：

```powershell
git pull
```

建议习惯：

- 换机器前：先 `git add .`、`git commit`、`git push`
- 开始工作前：先 `git pull`
- 尽量不要在两台机器同时修改同一个文件

## 项目级代理配置

如果某台机器访问 GitHub 不稳定，可以只给当前项目配置代理，不影响全局 Git。

先进入项目目录：

```powershell
cd D:\AutoReview
```

如果代理端口是 `33210`，执行：

```powershell
git config http.version HTTP/1.1
git config http.proxy http://127.0.0.1:33210
git config https.proxy http://127.0.0.1:33210
```

查看当前项目的 Git 配置：

```powershell
git config --local --list
```

如果这台机器不需要代理，可以取消当前项目代理：

```powershell
git config --unset http.proxy
git config --unset https.proxy
```

## 本项目不会同步的内容

项目 `.gitignore` 已经忽略这些本地文件：

```text
config/*.json
release/
tmp/
```

也就是说：

- `config/*.json` 是每台机器自己的真实配置，不会同步
- `config/*.example.json` 会同步，用来作为配置模板
- `release/` 和 `tmp/` 是构建产物或临时文件，不会同步

## 常用检查命令

查看当前分支和同步状态：

```powershell
git status --short --branch
```

查看远程仓库地址：

```powershell
git remote -v
```

查看最近提交：

```powershell
git log --oneline --decorate -5
```

## 如果 pull 时出现冲突

先查看冲突文件：

```powershell
git status
```

手动打开冲突文件，处理其中的冲突标记：

```text
<<<<<<< HEAD
本地内容
=======
远程内容
>>>>>>> 分支名
```

处理完成后提交：

```powershell
git add .
git commit -m "Resolve merge conflict"
git push
```
