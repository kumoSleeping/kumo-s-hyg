import json
from pathlib import Path
import time
import ssl
import threading
import traceback
from urllib.parse import urlencode
import qrcode
import requests
from typing import Optional, List, Dict
import json
from dataclasses import dataclass

from noneprompt import (
    InputPrompt,
    ConfirmPrompt,
    ListPrompt,
    CheckboxPrompt,
    Choice,
    CancelledError
)

from .log import logger
import yaml

from .api import Api
from .api import prepareJson, confirmJson, createJson, ProjectJson, BuyerJson, AddressJson


class Order:
    ssl._create_default_https_context = ssl._create_unverified_context

    def __init__(self, *, cookie: str, project_id: int = None) -> None:
        self.project_id: int = project_id
        self.api = Api(cookie=cookie)

        # self中的变量应该是最终构成 prepare / confirm / create 的参数
        self.screen_id: Optional[int] = None  # 场次
        self.sku_id: Optional[int] = None  # 票类
        self.pay_money: Optional[int] = None  # 票价（单位：分）
        self.buyer_info: Optional[List[Dict[str, str]]] = None  # 购票人信息 add 'isBuyerInfoVerified': 'true', 'isBuyerValid': 'true'
        self.count: Optional[int] = None  # 购票数量（需对应 buyer_info 数量）
        self.token: Optional[str] = None  # 开票前获取的token
        self.buyer = None  # 购票人姓名 （非实名制时使用）
        self.tel = None  # 购票人电话 （非实名制时使用）
        self.deliver_info: Optional[Dict[str, str]] = None  # 收货人信息
        self.timestamp = int(round(time.time() * 1000))

        
    def build(self, *, config: dict) -> None:
        '''
        ### param:
        - config: 购票配置, 具体见下方说明.
        
        ### 随便写点什么:
        - screen_idx: 场次索引.
        - ticket_index: 票种索引.
        - project_json: 项目json.
        - buyer_json: 购票人json. 实名制票按照此参数决定姓名和电话.
        - buyer_index_list: 购票人索引列表.
        - address_json: 地址json. 纸质票按照此地址下单; 非实名制票按照此参数决定姓名和电话.
        - address_index: 地址索引, 默认0.
        - count: 购票数量 实名制票此参数无效, 按照购票人数量自动判断; 非实名制按照此参数确定数量, 默认买一张.
        - project_json_by_date: 通过日期数据获得项目中的场次和票种信息, 只在日期票中使用, 例如 animate 咖啡.

        ### build:
        - 构建购票人信息(buyer_info / tel+name)
        - 构建地址信息 (纸质票)
        - 构建票种信息 main
        '''
        
        project_json = self.api.project(project_id=self.project_id)
        project_str = json.dumps(project_json, ensure_ascii=False)
        
        if 'buyer_index' in config and config['buyer_index']:
            if '一单一证' in project_str:
                buyer_index_list = config['buyer_index']
            elif '一人一证' in project_str:
                buyer_index_list = [config['buyer_index'][0]]
            buyer_json = self.api.buyer()

        screen_idx, ticket_idx = config['screen_ticket'][0]

        if 'buyer_index' in config and config['buyer_index']:
            if '一单一证' in project_str:
                buyer_index_list = config['buyer_index']
            elif '一人一证' in project_str:
                buyer_index_list = [config['buyer_index'][0]]
            buyer_json = self.api.buyer()
            
            buyer_info_list = []
            for i in buyer_index_list:
                buyer_info_raw: dict = buyer_json['data']["list"][i]
                buyer_info_raw.update({'isBuyerInfoVerified': 'true', 'isBuyerValid': 'true'}) 
                buyer_info_dict = buyer_info_raw
                buyer_info_list.append(buyer_info_dict)
                
            self.buyer_info = buyer_info_list
            self.count = len(self.buyer_info)
            
        elif 'address_index' in config and config['address_index']:
            address_index = config['address_index'][0]
            address_json = self.api.address()            # 非实名制
            self.buyer = address_json['data']['addr_list'][address_index]['name']
            self.tel = address_json['data']['addr_list'][address_index]['phone']
            self.count = config['count']

        # 构建票种信息
        if project_json['data']['sales_dates'] != []: # 存在小日历
            project_json_4_ticket = self.api.project_info_by_date(project_id=self.project_id, date=config['sales_date'][0])
        else:
            project_json_4_ticket = project_json
        
        self.screen_id = project_json_4_ticket['data']['screen_list'][screen_idx]['id']
        self.sku_id = project_json_4_ticket['data']['screen_list'][screen_idx]['ticket_list'][ticket_idx]['id']
        self.pay_money = project_json_4_ticket['data']['screen_list'][screen_idx]['ticket_list'][ticket_idx]['price']

        # 纸质票 处理 快递费
        if project_json['data']['has_paper_ticket'] and (express_fee := project_json["data"]['screen_list'][screen_idx]['express_fee']):
            if express_fee != -1: # free
                self.pay_money += express_fee
        # 纸质票 处理 地址
        if project_json["data"]['screen_list'][screen_idx]['delivery_type'] == 3: # 需要地址
            self.deliver_info = {
                "name": address_json["data"]["addr_list"][0]["name"],
                "tel": address_json["data"]["addr_list"][0]["phone"],
                "addr_id": address_json["data"]["addr_list"][0]["id"],
                "addr": address_json["data"]["addr_list"][0]["prov"] + address_json["data"]["addr_list"][0]["city"] + address_json["data"]["addr_list"][0]["area"] + address_json["data"]["addr_list"][0]["addr"],
                }
            
        # 构建时间
        self.sale_start = project_json['data']['screen_list'][screen_idx]['ticket_list'][ticket_idx]['saleStart']
        self.sale_end = project_json['data']['screen_list'][screen_idx]['ticket_list'][ticket_idx]['saleEnd']

    def prepare(self) -> prepareJson:
        prepare_json = self.api.prepare(
            project_id=self.project_id,
            count=self.count,
            screen_id=self.screen_id,
            sku_id=self.sku_id
        )
        if prepare_json["errno"] == 0:
            self.token = prepare_json["data"]["token"]
            return prepare_json
        else:
            logger.opt(colors=True).error(f"获取确认信息失败: {prepare_json['errno']}, {prepare_json['msg']}")
            return prepare_json
      
    def confirm(self) -> confirmJson:
        confirm_json = self.api.confirm(
            project_id=self.project_id,
            token=self.token,
            voucher="",
            request_source="pc-new"
        )
        if confirm_json["errno"] == 0:
            logger.opt(colors=True).success(f"获取确认信息成功, token: {self.token}")
        else:
            logger.opt(colors=True).error(f"获取确认信息失败: {confirm_json['errno']}, {confirm_json['msg']}")
        return confirm_json
    
    def create(self) -> createJson:
        create_json = self.api.create(
            project_id=self.project_id,
            token=self.token,
            screen_id=self.screen_id,
            sku_id=self.sku_id,
            count=self.count,
            pay_money=self.pay_money,
            buyer_info=self.buyer_info,
            deliver_info=self.deliver_info,
            buyer=self.buyer,
            tel=self.tel
        )
        return create_json
    