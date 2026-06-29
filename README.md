# 客户数据获取项目

## 项目简介

这是一个用于从阿里巴巴获取客户数据并同步到企业微信文档的项目。项目通过Flask提供Web界面，用户可以输入订单号，系统会自动从阿里巴巴获取相关数据，并将数据同步到企业微信文档中。

## 安装依赖

1. 确保你已经安装了Python 3.7或更高版本
2. 打开命令行工具，进入项目目录
3. 运行以下命令安装依赖：

```bash
pip install -r requirements.txt
```

## 运行项目

1. 确保依赖已经安装完成
2. 运行以下命令启动项目：

```bash
python app.py
```

3. 打开浏览器，访问 `http://localhost:5000` 即可使用

## 项目结构

- `app.py` - 项目的主入口文件，包含Flask应用和主要逻辑
- `data_processing.py` - 数据处理相关的功能
- `process_onetouch_to_wedoc.py` - 处理阿里巴巴数据同步到企业微信文档的功能
- `templates/` - 存放HTML模板文件
- `logs/` - 存放日志文件
- `[3656] request_onetouch-partner.alibaba.com_message.txt` - 请求抓包文件
- `[3656] response_onetouch-partner.alibaba.com_message.txt` - 响应样例文件

## 注意事项

1. 项目需要依赖请求抓包文件和响应样例文件来获取数据
2. 实时查询功能需要有效的ctoken和cookie信息
3. 同步到企业微信文档需要有效的webhook地址
4. 项目运行时会在logs目录下生成日志文件

## 使用说明

1. 在Web界面中输入阿里单号（多个单号可以换行输入）
2. 选择获取方式（响应文件或实时接口）
3. 选择是否同步到企业微信文档
4. 选择目标文档（港前或港后）
5. 点击"提交"按钮开始处理
6. 处理完成后，页面会显示处理结果
