# class Config:
#     def __init__(self):
#         self.model_name = "模型名"
#         self.api_key = "API-KEY"
#         self.base_url = "BASE_URL"

class Config:
    def __init__(self):
       self.model_name = "deepseek-v4-flash"
       self.api_key = "EMPTY"  # 本地部署服务不校验 key，占位即可
       self.base_url = "http://192.168.1.17:13001/v1"