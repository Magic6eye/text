'''
@Author:Sholway
@Usage:根据deptphc积压量来启动冷备服务器，达到阈值购买阿里云ECS服务器，并添加到NLP的负载均衡中，更改nacos配置。积压消失就停止和释放服务器
@Date:20231019
pip install alibabacloud_ecs20140526 alibabacloud_tea_console alibabacloud_slb20140515 alibabacloud_cms20190101 alibabacloud_sls20201230
'''

import json
import time
import requests
import pymysql
from alibabacloud_ecs20140526.client import Client as Ecs20140526Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_ecs20140526 import models as ecs_20140526_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_tea_console.client import Client as ConsoleClient
from alibabacloud_tea_util.client import Client as UtilClient
from alibabacloud_slb20140515.client import Client as Slb20140515Client
from alibabacloud_slb20140515 import models as slb_20140515_models
from alibabacloud_cms20190101.client import Client as Cms20190101Client
from alibabacloud_cms20190101 import models as cms_20190101_models

from alibabacloud_sls20201230.client import Client as Sls20201230Client
from alibabacloud_sls20201230 import models as sls_20201230_models

import datetime
from pytz import timezone
import nacos
import yaml


# 创建ECS连接对象
def create_ecs_client() -> Ecs20140526Client:
    config = open_api_models.Config(access_key_id=ALIBABA_CLOUD_ACCESS_KEY_ID, access_key_secret=ALIBABA_CLOUD_ACCESS_KEY_SECRET)
    config.endpoint = f'ecs-cn-hangzhou.aliyuncs.com'
    return Ecs20140526Client(config)


class Create_instance:
    @staticmethod
    def main(create_instance_number):
        client = create_ecs_client()
        system_disk = ecs_20140526_models.RunInstancesRequestSystemDisk(category='cloud_essd')
        tag_0 = ecs_20140526_models.RunInstancesRequestTag(key='nlp', value='')
        # 判断购买的服务器数量,一次API只能购买100台服务器，但是目前业务压力限制在一次只能购买5台，分批购买
        if create_instance_number > once_max_buy_servers_number:
            create_instance_list = []       #大量购买的服务器列表
            if create_instance_number > 499:
                create_instance_number = 495  # 超过最大购买数量，容错
                print('当前所选实例规格最多还可开通499台，超过可在阿里服务器购买页面提升配额。')
            remain_servers = create_instance_number
            while remain_servers > 0:
                servers_to_buy = min(remain_servers, once_max_buy_servers_number)
                run_instances_request = ecs_20140526_models.RunInstancesRequest(region_id='cn-hangzhou',image_id='m-bp1bocd8o3wlro32kskv',
                    instance_type='ecs.c7.xlarge',security_group_id='sg-bp1jcahu26j2a0x0z2pi',v_switch_id='vsw-bp1mhzii0iiilk4mp3ekw',instance_name='nlp-000',
                    description='nlp-000-python-auto-buy',host_name='nlp-000',password_inherit=True,unique_suffix=True,zone_id='cn-hangzhou-k',
                    min_amount=servers_to_buy,amount=servers_to_buy,
                    system_disk=system_disk,tag=[tag_0])
                runtime = util_models.RuntimeOptions()
                resp = client.run_instances_with_options(run_instances_request, runtime)
                if resp.status_code == 200:
                    create_instance_start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")      # 创建服务器的时间，用于后面判断新购买的服务器内存是否在跑
                    tmp_ecs_instance_ids = resp.body.instance_id_sets.instance_id_set  # 返回创建实例id列表
                    create_instance_list = create_instance_list + tmp_ecs_instance_ids
                    msg = f'分批任务,此次购买{servers_to_buy}台:\n{tmp_ecs_instance_ids}'
                    feishu(msg)
                    print('购买的添加到SLS日志服务中，防止日志丢失')
                    try:
                        print('需要等待服务器启动,不然下面检索不到新购买的服务器')
                        time.sleep(60)
                        resp_extract_ip = str(Selete_instance('all').body)
                        resp_extract_ip = resp_extract_ip.replace("'", '"').replace('True', '"True"').replace('False','"False"').replace('Null', '"None"')
                        parsed_data = json.loads(resp_extract_ip)
                        instances = parsed_data["Instances"]["Instance"]
                        primary_ip_addresses = [i['VpcAttributes']['PrivateIpAddress']['IpAddress'][0] for i in instances]
                        print(f'提取每个实例的PrimaryIpAddress列表{primary_ip_addresses}')
                        Modify_sls_servers.main(primary_ip_addresses, 'add')
                    except Exception as error:
                        print(f'replace后的resp:\n{resp_extract_ip}\n')
                        print(error)
                    time.sleep(30)
                    Modify_slb.main(tmp_ecs_instance_ids)
                    time.sleep(10)
                    publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
                else:
                    feishu(f'status_code!=200 resp:{resp}')
                remain_servers = remain_servers - servers_to_buy
                time.sleep(60)
                # 判断前面买的服务器内存是否占满了，是否要继续买新的服务器
                if remain_servers >= 10:
                    time.sleep(60)  # 确保服务器已经启动，NLP有加载运行到内存>50%，正常快速加载需要120s
                    # 取刚刚已经购买服务器的最后一台判断内存是否上涨，如果没有上涨说明有可能后端codeengine服务器应用出错
                    last_ecs_instance_id = tmp_ecs_instance_ids[-1]
                    try:
                        ecs_memory_dict = {'instanceId': last_ecs_instance_id, 'start_time': create_instance_start_time}
                        last_ecs_memory_average_value = WebMonitor_memory.main(ecs_memory_dict)
                        if last_ecs_memory_average_value < 50:
                            # 需要保证最后一个间隔至少120s后再判断（agent安装45s,加载nlp75s
                            feishu(f'异常!新购买的服务器:{last_ecs_instance_id},内存平均使用率:{last_ecs_memory_average_value}<50%\n停止购买服务器')
                            remain_servers = 0
                    except Exception as e:
                        print('create_instance_start_time可能没有值，说明上面并没有满足购买服务器的条件')
                        print(e)
                        remain_servers = 0

            msg = f'购买任务完成,总共购买{create_instance_number}台:\n{create_instance_list}'
            # feishu(msg)
            print(msg)
            tmp_ecs_instance_ids = create_instance_list
        elif create_instance_number > 0:
            run_instances_request = ecs_20140526_models.RunInstancesRequest(region_id='cn-hangzhou',image_id='m-bp1bocd8o3wlro32kskv',
                instance_type='ecs.c7.xlarge',security_group_id='sg-bp1jcahu26j2a0x0z2pi',v_switch_id='vsw-bp1mhzii0iiilk4mp3ekw',
                instance_name='nlp-000',description='nlp-000-python-auto-buy',host_name='nlp-000',
                password_inherit=True,unique_suffix=True,zone_id='cn-hangzhou-k',
                min_amount=create_instance_number,amount=create_instance_number,
                system_disk=system_disk,tag=[tag_0])
            runtime = util_models.RuntimeOptions()
            resp = client.run_instances_with_options(run_instances_request, runtime)
            if resp.status_code == 200:
                tmp_ecs_instance_ids = resp.body.instance_id_sets.instance_id_set   # 返回创建实例id列表
                msg = f'已购买ECS,共{create_instance_number}台:\n{tmp_ecs_instance_ids}'
                # print(msg)
                feishu(msg)
                time.sleep(5)
                print('购买的添加到SLS日志服务中，防止日志丢失')
                try:
                    resp_extract_ip = str(Selete_instance('all').body)
                    resp_extract_ip = resp_extract_ip.replace("'", '"').replace('True', '"True"').replace('False','"False"').replace('Null', '"None"')
                    # print(f'\n{resp_extract_ip}\n')
                    parsed_data = json.loads(resp_extract_ip)
                    instances = parsed_data["Instances"]["Instance"]
                    # 提取每个实例的 PrimaryIpAddress
                    primary_ip_addresses = [i['VpcAttributes']['PrivateIpAddress']['IpAddress'][0] for i in instances]
                    print(primary_ip_addresses)
                    Modify_sls_servers.main(primary_ip_addresses, 'add')
                except Exception as error:
                    print(f'\n{resp_extract_ip}\n')
                    print(error)
                time.sleep(30)
                Modify_slb.main(tmp_ecs_instance_ids)
                time.sleep(10)
                publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
            else:
                feishu(f'status_code!=200 resp:{resp}')
        print('等待ECS服务器启动')
        time.sleep(15)
        return tmp_ecs_instance_ids

class Modify_slb:
    @staticmethod
    def create_client() -> Slb20140515Client:
        config = open_api_models.Config(ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET)
        config.endpoint = f'slb.aliyuncs.com'
        return Slb20140515Client(config)

    @staticmethod
    def main(ecs_instance_ids):
        alb_backend_servers = []    # 添加到ALB服务器列表
        client = Modify_slb.create_client()

        for ecs_id in ecs_instance_ids:
            alb_backend_servers.append({ "ServerId": ecs_id, "Weight": "100", "Type": "ecs", "Port":"5000","Description":"python_auto_configure" })
        time.sleep(5)   # 防止服务器未启动添加SLB失败
        # 虚拟服务器组列表，单次最多可添加20个后端服务器
        if len(alb_backend_servers) > 20:
            print('ALB一次性超过20台,循环添加中...')
            while alb_backend_servers:
                tmp_alb_backend_servers = alb_backend_servers[:20]  # 取前20个出来添加到alb
                tmp_alb_backend_servers = str(tmp_alb_backend_servers).replace('\'', '"')    # append后，双引号变成了单引号，处理后格式才正确

                try:
                    add_vserver_group_backend_servers_request = slb_20140515_models.AddVServerGroupBackendServersRequest(
                        region_id='cn-hangzhou', backend_servers=tmp_alb_backend_servers,
                        vserver_group_id='rsp-bp19fu2513sfk'  # nlp服务器组
                    )
                    runtime = util_models.RuntimeOptions()
                    resp = client.add_vserver_group_backend_servers_with_options(add_vserver_group_backend_servers_request,runtime)
                except Exception as error:
                    print(error)
                    time.sleep(120)
                    add_vserver_group_backend_servers_request = slb_20140515_models.AddVServerGroupBackendServersRequest(
                        region_id='cn-hangzhou', backend_servers=tmp_alb_backend_servers,
                        vserver_group_id='rsp-bp19fu2513sfk'  # nlp服务器组
                    )
                    runtime = util_models.RuntimeOptions()
                    resp = client.add_vserver_group_backend_servers_with_options(
                        add_vserver_group_backend_servers_request, runtime)
                if resp.status_code == 200:
                    print(resp)
                    # feishu(f'已添加到ALB的NLP服务器组中：\n{ecs_instance_ids}')
                    del alb_backend_servers[:19]  # 删除从索引0到索引19的元素
                else:
                    feishu(f'status_code!=200 resp:{resp}')
            print('循环添加ALB完毕')
        else:
            alb_backend_servers = str(alb_backend_servers).replace('\'', '"')    # append后，双引号变成了单引号，处理后格式才正确

            try:
                add_vserver_group_backend_servers_request = slb_20140515_models.AddVServerGroupBackendServersRequest(
                    region_id='cn-hangzhou',
                    # backend_servers='[{ "ServerId": "i-xxxxxxxxx", "Weight": "100", "Type": "ecs", "Port":"5000","Description":"python_auto_configure" }]',
                    backend_servers=alb_backend_servers,
                    vserver_group_id='rsp-bp19fu2513sfk'    #nlp服务器组
                )
                runtime = util_models.RuntimeOptions()
                resp = client.add_vserver_group_backend_servers_with_options(add_vserver_group_backend_servers_request, runtime)
            except Exception as error:
                print(error)
                time.sleep(120)
                add_vserver_group_backend_servers_request = slb_20140515_models.AddVServerGroupBackendServersRequest(
                    region_id='cn-hangzhou',
                    backend_servers=alb_backend_servers,
                    vserver_group_id='rsp-bp19fu2513sfk'  # nlp服务器组
                )
                runtime = util_models.RuntimeOptions()
                resp = client.add_vserver_group_backend_servers_with_options(add_vserver_group_backend_servers_request, runtime)
            if resp.status_code == 200:
                try:
                    print(resp.body)
                except Exception as e:
                    print(f'print error:{e}')
                    feishu(e)
            else:
                feishu(f'status_code!=200 resp:{resp}')
        msg = f'已添加到ALB的NLP服务器组中:\n{ecs_instance_ids}'
        print(msg)
        # feishu(msg)
        return resp

    @staticmethod
    def removeInstanceFromSlb(ecs_instance_ids):
        client = Modify_slb.create_client()
        alb_backend_servers = []    # 添加到ALB服务器列表
        for ecs_id in ecs_instance_ids:
            alb_backend_servers.append({ "ServerId": ecs_id, "Weight": "100", "Type": "ecs", "Port":"5000","Description":"python_auto_configure" })

        request = slb_20140515_models.RemoveVServerGroupBackendServersRequest(
            region_id='cn-hangzhou',
            # backend_servers='[{ "ServerId": "i-xxxxxxxxx", "Weight": "100", "Type": "ecs", "Port":"5000","Description":"python_auto_configure" }]',
            backend_servers=alb_backend_servers,
            vserver_group_id='rsp-bp19fu2513sfk'  # nlp服务器组
        )
        resp = client.remove_vserver_group_backend_servers(request)
        print(resp)
        return resp

class WebMonitor_memory:
    @staticmethod
    def create_client() -> Cms20190101Client:
        config = open_api_models.Config(ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET)
        config.endpoint = f'metrics.cn-hangzhou.aliyuncs.com'
        return Cms20190101Client(config)

    @staticmethod
    def main(ecs_memory_dict):
        client = WebMonitor_memory.create_client()
        describe_metric_list_request = cms_20190101_models.DescribeMetricListRequest(
            namespace='acs_ecs_dashboard', metric_name='memory_usedutilization',
            #dimensions='[{"instanceId": "i-bp1d8yz8s0ktv0o7u1ce"}]',
            dimensions=f'[{{"instanceId": "{ecs_memory_dict["instanceId"]}"}}]',
            length='1440',
            start_time=ecs_memory_dict['start_time'],
            express='{"groupby":["timestamp","Average"]}'
        )
        runtime = util_models.RuntimeOptions()
        try:
            resp = client.describe_metric_list_with_options(describe_metric_list_request, runtime)
            # print(resp.body.datapoints)
            if resp.status_code == 200:
                if resp.body.datapoints != '':
                    average_memory_data_json = resp.body.datapoints
                    average_memory_data = json.loads(average_memory_data_json)
                    latest_average_memory = average_memory_data[-1]['Average']
                    print(f'latest_average_memory:{latest_average_memory}')
                    return int(latest_average_memory)
                else:
                    print('无内存监控数据')
            else:
                print('webmonitor error')
        except Exception as error:
            print(error)
            feishu(error)

class Modify_sls_servers:
    @staticmethod
    def create_client() -> Sls20201230Client:
        config = open_api_models.Config(ALIBABA_CLOUD_ACCESS_KEY_ID, ALIBABA_CLOUD_ACCESS_KEY_SECRET)
        config.endpoint = f'cn-hangzhou.log.aliyuncs.com'
        return Sls20201230Client(config)

    @staticmethod
    def main(modify_sls_servers_list, servers_action):
        client = Modify_sls_servers.create_client()
        # 冷备服务器ip,已经有了
        backup_server_ip_list = ['172.21.69.176', '172.21.69.122', '172.21.69.114']
        # for backup_server_ip in backup_server_ip_list:
        #     if backup_server_ip in modify_sls_servers_list:
        #         modify_sls_servers_list.remove(backup_server_ip)
        modify_sls_servers_list = [ip for ip in modify_sls_servers_list if ip not in backup_server_ip_list]
        if modify_sls_servers_list:     # 防止列表为空(没有检索到新购买的服务器
            update_machine_group_machine_request = sls_20201230_models.UpdateMachineGroupMachineRequest(
                # body=['192.168.1.1', '192.168.1.2'],
                # action='delete' or 'add
                body=modify_sls_servers_list,
                action=servers_action
            )
            runtime = util_models.RuntimeOptions()
            headers = {}
            try:
                resp = client.update_machine_group_machine_with_options('nlp-tohealth', 'nlp', update_machine_group_machine_request, headers, runtime)
                if resp.status_code == 200:
                    print(f'sls的resp:\n{resp}')
                    if servers_action == 'add':
                        print(f'已添加{modify_sls_servers_list}到SLS日志NLP服务器中')
                        # feishu(f'已添加{len(modify_sls_servers_list)}台\n{modify_sls_servers_list}到SLS日志NLP服务器中')
                    elif servers_action == 'delete':
                        print(f'已移除{len(modify_sls_servers_list)}台SLS日志中的NLP服务器\n{modify_sls_servers_list}')
                else:
                    # feishu(f'status_code!=200 resp:{resp}')
                    print(f'status_code!=200 resp:{resp}')
            except Exception as error:
                print(error)
                # feishu(error)
        else:
            print('modify_sls_servers_list empty')

def feishu(msg):
    data = {"msg_type": "text","content": {"text": f"{msg}"}}
    resp = requests.post(url=feishuWebhookURL, data=json.dumps(data))
    if resp.json()['StatusCode'] == 0 and resp.json()['StatusMessage'] == 'success':
        print(f'--------------------------------------------\n飞书发送消息:{msg}\n--------------------------------------------')

def getOverstockFromDB():   # 从数据库里面取积压数
    try:
        with pymysql.connect(host='rm-bp1onl331j81f3nc0.mysql.rds.aliyuncs.com',port=3306,user='dpws',password='LPKS8uXu',db='dpws_db') as conn:
            cursor = conn.cursor()
            # cursor.execute("select count(1) from pprs_task_log")
            cursor.execute("select count(1) from v_pprs_task_log_cnt")
            rows = cursor.fetchone()
            if rows is None:
                return 0
            # print(f'Now getOverstockFromDB number:{rows[0]}')
            return int(rows[0])
    except Exception as e:
        print(e)
        feishu(e)

def publicCPUToDeptPhcNacos(count):
    def publish(url):
        # no auth mode 因为namespace为默认public所以不传
        client = nacos.NacosClient(url)
        # client = nacos.NacosClient("http://10.92.100.253:8848")

        # get config
        data_id = "phc-server.yaml"
        group = "DEFAULT_GROUP"
        config = client.get_config(data_id, group)
        # print("old phc nacos config:\n",config)

        # update config
        kv = yaml.safe_load(config)
        kv['nlp']['cpu']["count"] = count

        # push config
        time.sleep(30)   # 缓冲时间
        newConfig = yaml.dump(kv)
        client.publish_config(data_id, group, newConfig, 5)
        config = client.get_config(data_id, group)
        # print("new phc nacos config:\n",config)
        msg = f'已更新nacos配置:{count}'
        # feishu(msg)
        print(msg)
        
    nacosList = ["http://172.21.69.105:8848","http://172.21.69.105:18848"]
    for url in nacosList:
        publish(url)


def Stop_instance(stop_instance_id_list):
    client = create_ecs_client()
    stop_instances_request = ecs_20140526_models.StopInstancesRequest(
        region_id='cn-hangzhou',stopped_mode='StopCharging',instance_id=stop_instance_id_list
        # instance_id=['i-bp1buxyqg8yta36ee289','i-bp1d8yz8s0ktv0o7u1ce','i-bp1jbuw3vtg0h3zm94sp']
    )
    runtime = util_models.RuntimeOptions()
    try:
        resp = client.stop_instances_with_options(stop_instances_request, runtime)
        if resp.status_code == 200:
            msg = f'已停止服务器:\n{stop_instance_id_list}'
            print(msg)
            # feishu(msg)
        else:
            feishu(f'status_code!=200 resp:{resp}')
    except Exception as error:
        # print(error)
        feishu(error)
    time.sleep(5)

def Start_instance(ecs_id):
    client = create_ecs_client()
    start_instance_request = ecs_20140526_models.StartInstanceRequest(instance_id=ecs_id)
    runtime = util_models.RuntimeOptions()
    try:
        resp = client.start_instance_with_options(start_instance_request, runtime)
        if resp.status_code == 200:
            # print(resp)
            msg = f'启动服务器:{ecs_id}'
            print(msg)
            # feishu(msg)
        else:
            feishu(f'status_code!=200 resp:{resp}')
    except Exception as error:
        print(error)
        feishu(error)
    time.sleep(15)

def Delete_instance(ecs_instance_ids):
    client = create_ecs_client()
    for e_i_i in ecs_instance_ids:  # 以下nlp服务器禁止删除,防止误删除
        if e_i_i in ['i-bp144worluccw8w54zv1','i-bp1buxyqg8yta36ee289','i-bp1d8yz8s0ktv0o7u1ce','i-bp1jbuw3vtg0h3zm94sp']:
            print('尝试删除冷备服务器，已阻止')
            ecs_instance_ids.remove(e_i_i)
            ecs_instance_ids = []
    delete_instances_request = ecs_20140526_models.DeleteInstancesRequest(region_id='cn-hangzhou', force=True,
        # instance_id=['i-bp1g6zv0ce8oghu7****','i-bp1g6zv0ce8oghu2']
        instance_id=ecs_instance_ids
    )
    runtime = util_models.RuntimeOptions()
    resp = client.delete_instances_with_options(delete_instances_request, runtime)
    if resp.status_code == 200:
        msg = f'已释放{len(ecs_instance_ids)}台NLP服务器:\n{ecs_instance_ids}'
        print(msg)
        msg = Modify_slb.removeInstanceFromSlb(ecs_instance_ids)
        print(msg)
        # feishu(msg)
    else:
        feishu(f'status_code!=200 resp:{resp}')
    time.sleep(15)
    return resp

def Selete_instance(select_status='Running'):
    client = create_ecs_client()
    tag_0 = ecs_20140526_models.DescribeInstancesRequestTag(key='nlp')
    if select_status == 'all':
        describe_instances_request = ecs_20140526_models.DescribeInstancesRequest(
            region_id='cn-hangzhou', tag=[tag_0], instance_name='nlp*', page_size=100, page_number=1)
    else:
        describe_instances_request = ecs_20140526_models.DescribeInstancesRequest(
            region_id='cn-hangzhou', tag=[tag_0], instance_name='nlp*', status='Running', page_size=100, page_number=1)
    runtime = util_models.RuntimeOptions()
    try:
        resp = client.describe_instances_with_options(describe_instances_request, runtime)
        if resp.status_code == 200:
            # for i in resp.body.instances.instance:
                # print(i.instance_id, i.status)
            # nlp_servers_count = resp.body.total_count
            # msg = f'当前共{nlp_servers_count}台服务器,修改nacos并发数{nlp_servers_cpu_count}'
            # print(msg)
            return resp
        else:
            feishu(f'API status_code != 200:{resp}')
    except Exception as error:
        print(error)
        feishu(f'API error:{error}')

def get_deptphc_nacos_cpu():
    client = nacos.NacosClient("http://172.21.69.105:8848")
    data_id = "phc-server.yaml"
    group = "DEFAULT_GROUP"
    config = client.get_config(data_id, group)
    kv = yaml.safe_load(config)
    cpu_count = kv['nlp']['cpu']["count"]
    return cpu_count


if __name__ == '__main__':
    feishuWebhookURL = "https://open.feishu.cn/open-apis/bot/v2/hook/707fb105-ea27-4283-8e77-a73551cf3dc7"  #deptphc监控群-xh机器人
    ALIBABA_CLOUD_ACCESS_KEY_ID = 'LTAI5t7vyZasybVwjWzEYRRn'
    ALIBABA_CLOUD_ACCESS_KEY_SECRET = 'cQwdxnsmR2PfunEvteaqFoaJJAo8HZ'
    one_server_nacos_configure = 6                  # 1台服务器配置nacos的数量
    one_server_average_process_logback = 10          # 1台服务器每分钟处理积压的个数
    finish_process_time = 15                       # 15分钟完成此次积压，1台每分钟大概处理5个积压
    once_max_buy_servers_number = 5                 # 最大一次能买多少台服务器
    total_buy_servers_number = 30                   # 最多总共买几台服务器

    while True:
        task_num_from_db = getOverstockFromDB()     # 取积压量
        start_instance_id_list = []                 # 冷备服务器启动列表
        ecs_instance_ids_all = []                   # 所有购买的服务器实例id
        # 判断当前时间，设置启动阈值,22:00-5:00之间提高阈值
        current_time = datetime.datetime.now(timezone('Asia/Shanghai')).time()  # datetime.time(10, 51, 49, 846197)
        if current_time >= datetime.time(19, 0) or current_time <= datetime.time(6, 0):
            logback_buy_server_number = 2000        # 晚天的阈值
            check_sleep_time = 600                  # 晚上每次循环检测的时间
        else:
            logback_buy_server_number = 300         # 白天的阈值
            check_sleep_time = 90                   # 白天每次循环检测的时间

        print('检查当前运行服务器CPU数量是否与Nacos合适,防止手工释放服务器造成不一致')
        nacos_cpu_number = get_deptphc_nacos_cpu()                  # 获取到nacoos里配置的cpu_count
        now_running_server = Selete_instance().body.total_count     # 当前运行的服务器数量
        update_nacos_cpu_number = now_running_server * one_server_nacos_configure   # 需要更新的nacos的cpu_count
        if nacos_cpu_number != update_nacos_cpu_number:     # 不一致说明可能存在手动释放服务器，导致不一致
            msg = f'当前运行服务器与nacos不匹配\nnacos的CPU配置:{nacos_cpu_number},运行的服务器数量:{now_running_server},需更新nacos配置到:{update_nacos_cpu_number}'
            print(msg)
            publicCPUToDeptPhcNacos(update_nacos_cpu_number)

        # 有2台nlp做冷备，有积压量就要启动
        msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 外循环,积压量:{task_num_from_db}'
        print(msg)
        if task_num_from_db > logback_buy_server_number:               # 有一台nlp服务器处理50个积压
            if task_num_from_db < logback_buy_server_number:
                print(f'50<logback< 当前门槛值{logback_buy_server_number}')
                print('判断激增情况')
                time.sleep(90)
                logback_increase_rate = int(getOverstockFromDB() / task_num_from_db)  # 激增率
                # 开启冷备服务器。  1k以下积压量，共有3台服务器处理：1台nlp长期开着。
                resp = Selete_instance('all')
                for ecs_server in resp.body.instances.instance:
                    print(f'{ecs_server.instance_id} {ecs_server.status}')
                    if ecs_server.status == "Stopped" and ecs_server.instance_id in ['i-bp1d8yz8s0ktv0o7u1ce','i-bp1jbuw3vtg0h3zm94sp']:
                        start_instance_id_list.append(ecs_server.instance_id)
                if start_instance_id_list:
                    if task_num_from_db >= 300 and logback_increase_rate >= 2:
                        print(f'激增较大，是原先的1倍以上:task_num_from_db >= 200 and logback_increase_rate >= 2')
                        start_instance_id_list = start_instance_id_list
                    elif task_num_from_db >= 200 and logback_increase_rate >= 1:   # len(start_instance_id_list) == 2说明当前已经有2台服务器再运行了，
                        print('激增不明显，只是有增长logback_increase_rate >= 1')
                        if len(start_instance_id_list) >= 2:
                            start_instance_id_list = start_instance_id_list[:2]
                        else:
                            start_instance_id_list = start_instance_id_list[:1]
                    # elif task_num_from_db >= 100 and len(start_instance_id_list) == 2:   # 150只需要2台服务器跑就行，其中1台是始终开的，只需要再开1台（开启前要判断当前已经有几台开启了
                    #     print('实际增长量不高 并且当前只有1台服务器再运行')
                    #     start_instance_id_list = start_instance_id_list[:1]
                    else:
                        print('没有增长')
                        start_instance_id_list = []
                    if start_instance_id_list:
                        msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 积压量:{getOverstockFromDB()}\n启动服务器:{start_instance_id_list}'
                        while start_instance_id_list:
                            Start_instance(start_instance_id_list[0])
                            del start_instance_id_list[0]
                        # print(msg)
                        feishu(msg)
                        time.sleep(20)   # 启动服务器需要时间
                        publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
                        print(f'等待刚启动的冷备服务器处理积压')
                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 50<积压量:{task_num_from_db}<当前门槛值{logback_buy_server_number}'
                print(msg)
                time.sleep(30)
            # 积压量达到多少就开始购买服务器,区分白天和晚上：白天1k,晚上3k
            elif task_num_from_db >= logback_buy_server_number:
                for ecs_server in Selete_instance('all').body.instances.instance:
                    if ecs_server.status == "Stopped" and ecs_server.instance_id in ['i-bp1d8yz8s0ktv0o7u1ce','i-bp1jbuw3vtg0h3zm94sp']:
                        print('购买服务器前需要把所有冷备服务器启动')
                        Start_instance(ecs_server.instance_id)
                        time.sleep(20)  # 等待服务器启动，防止未启动先算cpu
                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 积压量:{task_num_from_db},达到购买服务器阈值'
                # feishu(msg)
                print(msg)
                # 积压量需要在15分钟处理完：1台服务器大概1分钟处理5个积压量。1k积压量：15min*5个*13台=1k    15min*5个*6.6台=500
                create_instance_number = int(task_num_from_db / (finish_process_time * one_server_average_process_logback))   # 积压数/（15*5） :1000/(15*5)=13台
                if create_instance_number > total_buy_servers_number:                   # 总共只能购买30台服务器，超过就指定30台
                    create_instance_number = total_buy_servers_number
                already_running_ecs_number = Selete_instance().body.total_count - 3     # 现在正在运行的购买的服务器数量-3（不包括冷备服务器
                if already_running_ecs_number < create_instance_number:                 # 当前正在运行的服务器数量<需要购买的服务器数量，说明需要购买服务器
                    msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 积压量:{task_num_from_db}\n需要购买{create_instance_number}台服务器'
                    feishu(msg)
                    ecs_instance_ids = Create_instance.main(create_instance_number)         # 购买服务器
                    ecs_instance_ids_all = ecs_instance_ids_all + ecs_instance_ids          # 添加到总的服务器列表中，用于缓慢释放服务器
                    print('等待首次任务处理')
                    create_instance_start_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")      # 创建服务器的时间，用于后面判断新购买的服务器内存是否在跑
                else:
                    print('此脚本可能重启,不再购买服务器')
                time.sleep(120)     # 等待积压处理一段时间，后判断是否持续积压
                for i in range(0, 3):
                    msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 购买服务器处理后的积压量:{getOverstockFromDB()}'
                    if i == 0:
                        feishu(msg)
                    print(msg)
                    time.sleep(60)
                task_num_from_db = getOverstockFromDB()     # 积压处理后，继续判断是否要增加服务器
                time.sleep(120)  # 再等待处理，后判断是否持续积压
                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 再等待处理的积压量:{getOverstockFromDB()}'
                # feishu()
                print(msg)
                while True:
                    time.sleep(120)
                    task_num_from_db_now = getOverstockFromDB()
                    print(f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 开始内循环积压量:{task_num_from_db_now}')
                    if task_num_from_db_now > task_num_from_db and task_num_from_db_now > logback_buy_server_number:     # 当前比上次的积压量更大
                        msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} High:当前持续积压{task_num_from_db_now},上次积压量{task_num_from_db}'
                        print(msg)
                        # 进一步判断激增情况,添加服务器
                        logback_increase_rate = float(getOverstockFromDB() / task_num_from_db)    # 激增率
                        logback_increase_number = int(getOverstockFromDB() - task_num_from_db)  # 增长量
                        print(f'now logback_increase_rate:{logback_increase_rate},logback_increase_number:{logback_increase_number}')
                        if logback_increase_rate > 1:
                            if logback_increase_rate >= 2:      # 激增较大，是原先的1倍以上
                                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} High:当前积压激增较高{task_num_from_db_now},上次积压量{task_num_from_db}'
                            elif logback_increase_rate > 1:     # 激增超过购买服务器阈值
                                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} High:当前积压平缓升高{task_num_from_db_now},上次积压量{task_num_from_db}'
                            # print(msg)
                            feishu(msg)
                            create_instance_number = int(logback_increase_number / (finish_process_time * one_server_average_process_logback))  # 当前积压数/（15*5）:1000/(15*5)=13台
                            already_buy_ecs_number = Selete_instance('all').body.total_count - 3                # 减去原有的3台冷备服务器
                            if already_buy_ecs_number + create_instance_number > total_buy_servers_number:      # 限制服务器购买的数量
                                can_buy_ecs_number = total_buy_servers_number - already_buy_ecs_number          # 现在能购买的数量
                                print(f'购买的服务器总数量>{total_buy_servers_number},这次能购买的数量:{can_buy_ecs_number}')
                                create_instance_number = can_buy_ecs_number
                        else:
                            print('<=1 积压下降')
                            create_instance_number = 0
                        # 取刚刚已经购买服务器的最后一台判断内存是否上涨，如果没有上涨说明有可能后端codeengine服务器应用出错
                        last_ecs_instance_id = ecs_instance_ids[-1]
                        try:
                            ecs_memory_dict = {'instanceId': last_ecs_instance_id, 'start_time': create_instance_start_time}
                            last_ecs_memory_average_value = WebMonitor_memory.main(ecs_memory_dict)
                            if last_ecs_memory_average_value < 50:
                                feishu(f'异常!新购买的服务器:{last_ecs_instance_id},内存平均使用率:{last_ecs_memory_average_value}<50%')    # 需要保证最后一个间隔至少120s后再判断（agent安装45s,加载nlp75s
                                create_instance_number = 0
                        except Exception as e:
                            print('create_instance_start_time可能没有值，说明上面并没有满足购买服务器的条件')
                            print(e)

                        if create_instance_number > 0:
                            ecs_instance_ids = Create_instance.main(create_instance_number)  # 购买服务器
                            ecs_instance_ids_all = ecs_instance_ids_all + ecs_instance_ids  # 添加到总的服务器列表中，用于缓慢释放服务器

                        time.sleep(120)  # 等待分钟处理任务
                    elif task_num_from_db_now < 250:
                        # task_num_from_db_now = getOverstockFromDB()
                        msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Middle<250:积压量{task_num_from_db_now}'
                        print(msg)
                        # feishu(msg)
                        print('<250')
                        logback_decrease_rate = 0
                        if getOverstockFromDB() < 250:
                            print('getOverstockFromDB() < 50 去外循环释放所有购买的服务器')
                            break
                        time.sleep(180)     # 为下面计算降低率做准备
                        try:
                            now_logback = getOverstockFromDB()
                            logback_decrease_rate = float(now_logback / task_num_from_db)    # 降低率
                            print(f'logback_decrease_rate:{now_logback}/{task_num_from_db}={logback_decrease_rate}')
                        except Exception as error:
                            print(error)
                            print(f'除数为0')
                            break   # 跳出循环，进行释放服务器操作
                        # 下降率<1说明在下降，下降0.5说明下降比较剧烈 和积压<300都要释放服务器
                        if logback_decrease_rate < 1 or getOverstockFromDB() < 300:
                            delete_instance_number = 0
                            if len(ecs_instance_ids_all) >= 1 or int(len(ecs_instance_ids_all)/2) > 0:
                                if len(ecs_instance_ids_all) == 1:
                                    delete_instance_number = 1
                                elif getOverstockFromDB() < 100:
                                    print('getOverstockFromDB() < 60 去外循环释放所有购买的服务器')
                                    break
                                elif logback_decrease_rate < 0.4 or getOverstockFromDB() < 100:       # 下降0.5说明下降比较剧烈
                                    delete_instance_number = int(len(ecs_instance_ids_all)/2)  # 释放一半服务器
                                else:
                                    if int(len(ecs_instance_ids_all)/4) > 0:
                                        delete_instance_number = int(len(ecs_instance_ids_all)/4)
                                        print('下降量不明显，释放几台服务器')
                                    else:
                                        delete_instance_number = 0
                            if len(ecs_instance_ids_all) >= delete_instance_number and delete_instance_number:
                                Delete_instance(ecs_instance_ids_all[-delete_instance_number:])
                                del ecs_instance_ids_all[-delete_instance_number:]
                                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Middle:积压下降,当前:{now_logback},上次积压量{task_num_from_db}\n已释放{delete_instance_number}台\n{ecs_instance_ids_all[-delete_instance_number:]}'
                                feishu(msg)
                                time.sleep(10)
                                publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)

                            if getOverstockFromDB() < 100:
                                break
                            time.sleep(120)  # 等待分钟处理任务
                        else:
                            print('不满足logback_decrease_rate < 1 and getOverstockFromDB() < 250')
                        time.sleep(120)  # 等待分钟处理任务
                        print(f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 经过缓慢释放判断后的积压量:{getOverstockFromDB()}')
                    else:   # 少于500就释放全部服务器了
                        if getOverstockFromDB() < 200:
                            msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Low:<200积压量:{getOverstockFromDB()}'
                            print(msg)
                            # feishu(msg)

                            this_time_stop_instance_id_list = []
                            resp = Selete_instance()
                            for ecs_server in resp.body.instances.instance:
                                print('logback < 200:' + ecs_server.instance_id, ecs_server.status)
                                if ecs_server.status == "Running" and ecs_server.instance_id in ['i-bp1d8yz8s0ktv0o7u1ce']:     # nlp2 , 'i-bp1jbuw3vtg0h3zm94sp'
                                    this_time_stop_instance_id_list.append(ecs_server.instance_id)

                            if 150 < task_num_from_db_now < 200:
                                this_time_stop_instance_id_list = this_time_stop_instance_id_list[:1]
                            elif task_num_from_db_now < 100:
                                this_time_stop_instance_id_list = this_time_stop_instance_id_list[:2]
                            elif task_num_from_db_now < 50:
                                this_time_stop_instance_id_list = this_time_stop_instance_id_list[:3]
                            else:
                                this_time_stop_instance_id_list = []
                                print('500-1000内的任务，等待时间继续处理.')

                            if this_time_stop_instance_id_list:
                                Stop_instance(this_time_stop_instance_id_list)
                                publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
                                msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Low:积压量:{getOverstockFromDB()}\n已停止{len(this_time_stop_instance_id_list)}台服务器\n{this_time_stop_instance_id_list}'
                                # print(msg)
                                feishu(msg)

                        # < 200 要停止服务器和释放服务器
                        time.sleep(120)     # 缓冲时间
                        if getOverstockFromDB() > logback_buy_server_number:
                            continue
                        elif getOverstockFromDB() < 100:
                            break  # 去外循环处理
                        else:
                            time.sleep(100)

                    task_num_from_db = getOverstockFromDB()  # 积压处理后，继续判断是否要增加服务器
                    time.sleep(120)
            time.sleep(30)  # 缓冲时间
        else:
            print('积压小于<',logback_buy_server_number)
            if task_num_from_db > 100:
                print("继续等待一个周期，不要频繁重启")
                time.sleep(check_sleep_time)
                continue
            # 释放和停止服务器
            this_time_delete_instance_id_list = []
            this_time_stop_instance_id_list = []
            resp = Selete_instance()
            for ecs_server in resp.body.instances.instance:
                print(ecs_server.instance_id, ecs_server.status)  # 此服务器为长期开启'i-bp144worluccw8w54zv1',其它冷备服务器也禁止删除
                if ecs_server.status == "Running" and ecs_server.instance_id not in ['i-bp144worluccw8w54zv1','i-bp1d8yz8s0ktv0o7u1ce','i-bp1jbuw3vtg0h3zm94sp']:
                    this_time_delete_instance_id_list.append(ecs_server.instance_id)
                if ecs_server.status == "Running" and ecs_server.instance_id in ['i-bp1d8yz8s0ktv0o7u1ce']:     # nlp2 ,'i-bp1jbuw3vtg0h3zm94sp'
                    this_time_stop_instance_id_list.append(ecs_server.instance_id)
            if this_time_delete_instance_id_list or this_time_stop_instance_id_list:
                print('准备停止和释放所有购买的服务器')
                logback_number = getOverstockFromDB()
                if this_time_delete_instance_id_list:
                    try:
                        resp_extract_ip = str(Selete_instance('all').body)
                        resp_extract_ip = resp_extract_ip.replace("'", '"').replace('True', '"True"').replace('False','"False"').replace('Null', '"None"')
                        parsed_data = json.loads(resp_extract_ip)
                        instances = parsed_data["Instances"]["Instance"]
                        primary_ip_addresses = [i['VpcAttributes']['PrivateIpAddress']['IpAddress'][0] for i in instances]
                        print(f'准备删除SLS日志服务中要释放的服务器IP:{primary_ip_addresses}')
                    except Exception as error:
                        print(error)
                    Delete_instance(this_time_delete_instance_id_list)
                    msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Low:积压量:{logback_number}\n已释放所有购买的服务器共{len(this_time_delete_instance_id_list)}台\n{this_time_delete_instance_id_list}'
                    feishu(msg)
                    publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
                    Modify_sls_servers.main(primary_ip_addresses, 'delete')

                time.sleep(5)
                logback_number = getOverstockFromDB()
                if logback_number < 50 and this_time_stop_instance_id_list:
                    Stop_instance(this_time_stop_instance_id_list)
                    publicCPUToDeptPhcNacos(Selete_instance().body.total_count * one_server_nacos_configure)
                    msg = f'{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")} Low:积压量:{logback_number}\n已停止服务器:{this_time_stop_instance_id_list}'
                    feishu(msg)

        # 无积压任务,间隔巡检一次任务
        time.sleep(check_sleep_time)
