#!/usr/bin/env python3

import argparse
from collections import OrderedDict
import configparser
from dataclasses import dataclass
import os
import subprocess
import json
from datetime import datetime, timedelta, timezone
import threading
import time
import tkinter as tk
from tkinter import ttk

@dataclass
class EC2InstanceConfig:
    id: str
    display_name: str
    user: str
    directory: str | None


@dataclass
class EC2InstanceStatus:
    config: EC2InstanceConfig
    id: str
    name: str
    state: str
    public_ip: str | None
    elapsed_time: timedelta | None


EC2InstanceConfigCollection = OrderedDict[str, EC2InstanceConfig]
EC2InstanceStatusCollection = OrderedDict[str, EC2InstanceStatus]

def get_ec2_instance_configs(ini_path) -> EC2InstanceConfigCollection:
    if not os.path.exists(ini_path):
        raise FileNotFoundError(f"{ini_path} not found")

    ini = configparser.ConfigParser()
    instances: EC2InstanceConfigCollection = OrderedDict()
    with open(ini_path) as f:
        ini.read_file(f)
        for section_name in ini.sections():
            section = ini[section_name]
            id = section.get('id')
            if not id:
                continue
            instances[id] = EC2InstanceConfig(
                id = id,
                display_name = section_name,
                user = section.get('user', 'ec2-user'),
                directory = section.get('directory', None)
            )
    return instances


def get_ec2_instance_states(config: EC2InstanceConfigCollection) -> EC2InstanceStatusCollection:
    instance_ids = config.keys()
    command = [
        'aws', 'ec2', 'describe-instances',
        '--instance-ids', *instance_ids,
        '--query', "Reservations[*].Instances[*].[InstanceId, Tags[?Key=='Name'].Value | [0], State.Name, PublicIpAddress, LaunchTime]",
        '--output', 'json'
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr)
    out = json.loads(result.stdout)

    instances: EC2InstanceStatusCollection = OrderedDict()
    current_time = datetime.now(timezone.utc)
    for reservation in out:
        for items in reservation:
            id = items[0]
            config_item = config[id]
            launch_time = datetime.strptime(items[4], '%Y-%m-%dT%H:%M:%S%z').replace(tzinfo=timezone.utc)
            state = items[2]
            elapsed_time = current_time - launch_time if state == 'running' else None
            instances[id] = EC2InstanceStatus(
                config = config_item,
                id = id,
                name = items[1] if items[1] else config_item.display_name,
                state = state,
                public_ip = items[3] if items[3] else None,
                elapsed_time = elapsed_time
            )
    return instances


def _send_command(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(result.stderr)
    out = json.loads(result.stdout)
    for item in out:
        # TODO このタイミングでビューに変化を起こしたいところだけど、もっと変数のまとまりを整理してから
        print(f"{item[0]} is {item[1]}")

def start_ec2_instance(instance: EC2InstanceStatus):
    _send_command([
        'aws', 'ec2', 'start-instances',
        '--instance-ids', instance.config.id,
        '--query', "StartingInstances[*].[InstanceId, CurrentState.Name]",
        '--output', 'json'
    ])
    burst_status_watching()


def stop_ec2_instance(instance: EC2InstanceStatus):
    _send_command([
        'aws', 'ec2', 'stop-instances',
        '--instance-ids', instance.config.id,
        '--query', "StoppingInstances[*].[InstanceId, CurrentState.Name]",
        '--output', 'json'
    ])
    burst_status_watching()


def open_vscode_remote_ssh(instance: EC2InstanceStatus):
    command = [
        'code', '--new-window', '--remote',
        f'ssh-remote+{instance.config.user}@{instance.public_ip}'
    ]
    if instance.config.directory:
        command.append(instance.config.directory)
    subprocess.run(command)


def possible_actions(instance: EC2InstanceStatus):
    actions = dict(start=False, stop=False, vscode=False)
    if instance.state == 'running':
        actions['stop'] = True
        if instance.public_ip:
            actions['vscode'] = True
    if instance.state == 'stopped':
        actions['start'] = True
    return actions

def format_elapsed_time(t: timedelta | None) -> str:
    if t is None:
        return ''
    hours, remainder = divmod(t.total_seconds(), 3600)
    minutes, second = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02}:{int(second):02}"


def init_treeview(tree: ttk.Treeview, config: EC2InstanceConfigCollection):
    for instance in config.values():
        tree.insert('', 'end', instance.id, values=(
            instance.id,
            instance.display_name,
            '',
            '',
            ''
        ))

def update_treeview(
    states: EC2InstanceStatusCollection,
    tree: ttk.Treeview
):
    for instance in states.values():
        tree.item(instance.id, values=(
            instance.id,
            instance.name,
            instance.state,
            instance.public_ip if instance.public_ip else '',
            format_elapsed_time(instance.elapsed_time)
        ))

def update_instance_status(
    config: EC2InstanceConfigCollection,
    states: EC2InstanceStatusCollection,
    tree: ttk.Treeview
):
    new_states = get_ec2_instance_states(config)
    for instance in new_states.values():
        states[instance.id] = instance
    update_treeview(states, tree)

continue_watching = True
status_watching_burst = 0
status_watching_immidiate = False

def status_watching_worker(
    config: EC2InstanceConfigCollection,
    states: EC2InstanceStatusCollection,
    tree: ttk.Treeview,
    default_interval=60,
    burst_interval=6
):
    update_instance_status(config, states, tree)
    global status_watching_burst, status_watching_immidiate
    tick = 0
    while continue_watching:
        interval = default_interval if status_watching_burst <= 0 else burst_interval
        tick += 1
        if tick % interval == 0 or status_watching_immidiate:
            update_instance_status(config, states, tree)
            status_watching_burst -= 1 if status_watching_burst > 0 else 0
            status_watching_immidiate = False
        else:
            for instance in states.values():
                if instance.elapsed_time:
                    instance.elapsed_time += timedelta(seconds=1)
            update_treeview(states, tree)
        time.sleep(1)


def burst_status_watching():
    global status_watching_burst, status_watching_immidiate
    status_watching_burst = 10
    status_watching_immidiate = True


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        prog='python main.py',
        description='EC2 Power Switch'
    )
    arg_parser.add_argument('-c', '--config', dest='config', help='Path to the ini file (default: instances.ini)', default='instances.ini')
    args = arg_parser.parse_args()

    ec2_configs = get_ec2_instance_configs(args.config)
    ec2_states = OrderedDict()

    root = tk.Tk()
    root.title("EC2 Power Switch")

    # 表を作成
    tree = ttk.Treeview(root)
    tree["columns"] = (1, 2, 3, 4, 5)
    tree["show"] = "headings"
    tree.heading(1, text="インスタンスID")
    tree.heading(2, text="名前")
    tree.heading(3, text="状態")
    tree.heading(4, text="IPアドレス")
    tree.heading(5, text="経過時間")
    init_treeview(tree, ec2_configs)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def selected_instance_state():
        sel = tree.selection()
        if not sel:
            return None
        s = ec2_states[tree.item(sel[0], 'values')[0]]
        return s

    def do_with_selected_instance(func):
        def wrapper():
            s = selected_instance_state()
            if s:
                func(s)
        return wrapper

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="起動", command=do_with_selected_instance(start_ec2_instance))
    menu.add_command(label="停止", command=do_with_selected_instance(stop_ec2_instance))
    menu.add_command(label="VSCode Remote SSH", command=do_with_selected_instance(open_vscode_remote_ssh))
    menu.add_separator()
    menu.add_command(label="更新", command=lambda: update_instance_status(ec2_configs, ec2_states, tree))

    def show_menu(e):
        item = tree.identify_row(e.y)
        if item:
            tree.selection_set(item)
            actions = possible_actions(selected_instance_state())
            menu.entryconfig(0, state=tk.NORMAL if actions['start'] else tk.DISABLED)
            menu.entryconfig(1, state=tk.NORMAL if actions['stop'] else tk.DISABLED)
            menu.entryconfig(2, state=tk.NORMAL if actions['vscode'] else tk.DISABLED)
            menu.post(e.x_root, e.y_root)

    tree.bind("<Button-2>", show_menu)

    # update_instance_status(ec2_configs, ec2_states, tree)
    thread = threading.Thread(
        target=status_watching_worker,
        args=(ec2_configs, ec2_states, tree, 60, 6)
    )
    thread.start()

    root.mainloop()

    continue_watching = False
    thread.join()
