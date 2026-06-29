import json
import re
import requests
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 读取响应文件
def read_response_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 提取JSON部分
        json_match = re.search(r'\{"code":.*', content, re.DOTALL)
        if json_match:
            json_str = json_match.group(0)
            return json.loads(json_str)
        else:
            logging.error("未找到响应文件中的JSON数据")
            return None
    except Exception as e:
        logging.error(f"读取响应文件时出错: {str(e)}")
        return None

# 提取需要的字段
def extract_fields(response_data, order_number):
    try:
        data_list = response_data.get('data', {}).get('dataList', [])
        for item in data_list:
            if item.get('orderNumber') == order_number:
                # 提取字段
                fields = {
                    'email': None,  # 响应中未提供
                    'mobileNo': None,  # 响应中未提供
                    'sellerLoginId': item.get('sellerLoginId'),
                    'customerName': item.get('customerName'),
                    'solutionName': '半托管' if '半托管' in item.get('solutionName', '') else '非半托管',
                    'orderNumber': item.get('orderNumber')
                }
                # 检查必要字段
                required_fields = ['sellerLoginId', 'customerName', 'orderNumber']
                for field in required_fields:
                    if fields[field] is None:
                        logging.warning(f"缺少必要字段: {field}")
                return fields
        logging.error(f"未找到订单号为{order_number}的记录")
        return None
    except Exception as e:
        logging.error(f"提取字段时出错: {str(e)}")
        return None

# 构造企业微信文档数据
def construct_wechat_data(fields):
    try:
        if not fields:
            return None
        
        # 构造数据
        data = {
            "schema": [
                {"field": "flosGy", "name": "订单号"},
                {"field": "f3Vstd", "name": "下单账号"},
                {"field": "fyNfPg", "name": "用户名称"},
                {"field": "f6KONI", "name": "电话"},
                {"field": "fE2nO0", "name": "邮箱"},
                {"field": "faGx63", "name": "半托管/非半托管"},
                {"field": "fabcde", "name": "其他字段1"},
                {"field": "fA8znU", "name": "其他字段2"},
                {"field": "fg4QC7", "name": "其他字段3"}
            ],
            "add_records": [
                {
                    "flosGy": fields.get('orderNumber', ''),
                    "f3Vstd": fields.get('sellerLoginId', ''),
                    "fyNfPg": fields.get('customerName', ''),
                    "f6KONI": fields.get('mobileNo', ''),
                    "fE2nO0": fields.get('email', ''),
                    "faGx63": [{"text": fields.get('solutionName', '非半托管')}],
                    "fabcde": "",
                    "fA8znU": "",
                    "fg4QC7": ""
                }
            ]
        }
        return data
    except Exception as e:
        logging.error(f"构造企业微信文档数据时出错: {str(e)}")
        return None

# 发送数据到webhook
def send_to_webhook(data, webhook_url):
    try:
        headers = {'Content-Type': 'application/json'}
        response = requests.post(webhook_url, json=data, headers=headers)
        response.raise_for_status()
        logging.info(f"数据发送成功: {response.text}")
        return True
    except Exception as e:
        logging.error(f"发送数据到webhook时出错: {str(e)}")
        return False

# 主函数
def main():
    # 响应文件路径
    response_file = r'tmp\[3656] response_onetouch-partner.alibaba.com_message.txt'
    # 企业微信webhook地址
    webhook_url = 'https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=3OeXQfxTesRYhrHFn8IvM4Q8egvx1oal4aVhfBJ8jfo5xvh1CpJ2zkEsW5aGLcGnrg4djwooLpMmLZrtqQBopCZqxIhPzEww4SrZTkDDtUza'
    # 阿里单号
    order_number = 'ALS01481544388'
    
    # 读取响应文件
    response_data = read_response_file(response_file)
    if not response_data:
        logging.error("无法读取响应数据")
        return
    
    # 提取字段
    fields = extract_fields(response_data, order_number)
    if not fields:
        logging.error("无法提取字段")
        return
    
    # 构造企业微信文档数据
    wechat_data = construct_wechat_data(fields)
    if not wechat_data:
        logging.error("无法构造企业微信文档数据")
        return
    
    # 发送数据到webhook
    send_to_webhook(wechat_data, webhook_url)

if __name__ == "__main__":
    main()
