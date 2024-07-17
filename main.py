# Import necessary libraries
import time
import json
import math
import random
import requests
import urllib.parse

# Import specific modules from the hyperliquid library
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Import Ethereum account management library
import eth_account
from eth_account.signers.local import LocalAccount
    
# Import custom utility functions 
import utils

def main():
    file = "sl.json"  # File to store stop-loss information
    config = utils.get_config()  # Load configuration settings
    bot_token = config["bot_token"]  # Bot token for Telegram notifications
    chat_id = config["chat_id"]  # Chat ID for Telegram notifications
    sl = read(file)  # Read stop-loss value from file
    amount = config["spot_amount"]  # Amount of USDC to trade
    coin = config["spot_coin"]  # Coin to trade
    sz_decimals = None  # Decimal precision for the coin size
    wei_decimals = None  # Decimal precision for price
    index = None  # Coin index
    name = None  # Coin name
    
    # Initialize Info object for accessing market information
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    meta = info.spot_meta()  # Get market metadata
    
    # Find the required coin information from metadata
    for asset in meta["tokens"]:
        if asset["name"] == coin:
            sz_decimals = asset["szDecimals"]
            wei_decimals = asset["weiDecimals"]
            index = asset["index"]
            break
    if sz_decimals is None or index is None or wei_decimals is None:
        raise Exception(f"Could not find coin {coin} in tokens")
    
    # Find the coin name in the market universe from its index to get the asset name (required for placing orders)
    for asset in meta["universe"]:
        if asset["tokens"][0] == index:
            name = asset["name"]
            break
    if name is None:
        raise Exception(f"Could not find coin {coin} in universe")
    
    # Initialize Ethereum account and exchange object
    account: LocalAccount = eth_account.Account.from_key(config["secret_key"])
    address = config["address"]
    exchange = Exchange(account, constants.MAINNET_API_URL)    
    
    print("Running with account address:", address)
    
    while True:
        # Get account balances
        balance_coin = 0
        balance_usdc = 0
        spot_user_state = exchange.info.spot_user_state(address)
        if len(spot_user_state["balances"]) > 0:
            for balance in spot_user_state["balances"]:
                if balance["coin"] == coin:
                    balance_coin = truncate(float(balance["total"]), sz_decimals)
                elif balance["coin"] == "USDC":
                    balance_usdc = float(balance["total"])
        else:
            print("No available token balances")
        print(f"Balance {coin}: {balance_coin}")
        print(f"Balance USDC: {balance_usdc}")
 
        # Get current price of the coin
        px = float(exchange.info.all_mids()[name])
        price = round(float(f"{px:.5g}"), 6)
        print(f"Price: {price}")

        # If coin balance is zero and stop-loss is active, set stop-loss to zero (this situation is to detect when the TP is hit)
        if balance_coin == 0 and sl > 0:
            sl = 0
            write(file, sl)
            send_message(bot_token, chat_id, f'CLOSED POSITION: {coin}\nSL: {sl}')
            cancel_all(exchange, coin, name, address, info)
            time.sleep(5000) 

        # Open new position if conditions are met 
        # (with a random chance of 20%, to add some randomness in the behavior)
        if balance_coin == 0 and balance_usdc >= 100 and random.random() < 0.2:
            print("--OPENING--")

            # Calculate size to trade based on available balance and price, 
            # and the specified amount from the configuration file
            if amount == 0 or amount > balance_usdc:
                sz = truncate(balance_usdc / price, sz_decimals)
            else:
                sz = truncate(amount / price, sz_decimals)
            print(f"Size: {sz}")

            cancel_all(exchange, coin, name, address, info)
            time.sleep(5)
            sl = buy(exchange, coin, name, sz, sz_decimals, bot_token, chat_id, address)
            write(file, sl)

        # Close position if price drops below stop-loss
        elif balance_coin > 0 and px < sl:
            print("--CLOSING--")
            cancel_all(exchange, coin, name, address, info)
            sell(exchange, coin, name, balance_coin, bot_token, chat_id)
            sl = 0
            write(file, sl)
            time.sleep(5000)

        print("\n--WAITING--")

        # Adjust sleep time based on whether stop-loss is active 
        if sl > 0: 
            time.sleep(30) # If stop-loss is active, check every 30 seconds to see if it is hit
        else:
            time.sleep(300) # If no position is open, wait for 5 minutes before checking again if conditions are met to open a new one

# Helper function to truncate a number to a specified number of decimals
def truncate(number, decimals):
    factor = 10.0 ** decimals
    return math.floor(number * factor) / factor

# Function to execute a market buy order
def buy(exchange: Exchange, coin, name, size, sz_decimals, bot, chat_id, address):
    print(f"We try to Market buy {size} {coin}.")
    order_result = exchange.market_open(name, True, size)
    if order_result["status"] == "ok":
        for status in order_result["response"]["data"]["statuses"]:
            try:
                filled = status["filled"]
                print(f'Order #{filled["oid"]} filled {filled["totalSz"]} @{filled["avgPx"]}')
                send_message(bot, chat_id, f'OPENED POSITION:'
                             f'Filled {filled["totalSz"]} {coin} @ {filled["avgPx"]} (order #{filled["oid"]})'
                             f'SL: {float(filled["avgPx"]) * 0.96}')
            except KeyError:
                print(f'Error: {status["error"]}')
    
    balance_coin = 0
    spot_user_state = exchange.info.spot_user_state(address)
    if len(spot_user_state["balances"]) > 0:
        for balance in spot_user_state["balances"]:
            if balance["coin"] == coin:
                balance_coin = truncate(float(balance["total"]), sz_decimals)
                sz = balance_coin

    # Add Take Profit and return Stop Loss (at 4% from the entry price)
    px_tp = float(filled['avgPx']) * 1.04
    px_sl = float(filled['avgPx']) * 0.96
    print(f"We try to Add Take Profit.")
    tp_result = exchange.order(name, False, sz, round(float(f"{px_tp:.5g}"), 6), reduce_only=False, order_type={"limit": {"tif": "Gtc"}})
    print(tp_result)
    print(f"SL: {px_sl}")
    return px_sl
       
# Function to execute a market sell order
def sell(exchange: Exchange, coin, name, size, bot, chat_id):
    print(f"We try to Market sell {coin}.")
    order_result = exchange.market_open(name, False, size)
    if order_result["status"] == "ok":
        for status in order_result["response"]["data"]["statuses"]:
            try:
                filled = status["filled"]
                print(f'Order #{filled["oid"]} filled {filled["totalSz"]} @{filled["avgPx"]}')
                send_message(bot, chat_id, f'CLOSED POSITION:'
                             f'Filled {filled["totalSz"]} {coin} @ {filled["avgPx"]} (order #{filled["oid"]} )')
            except KeyError:
                print(f'Error: {status["error"]}')

# Function to cancel all open orders for a specific coin
def cancel_all(exchange: Exchange, coin, name, address, info: Info):
    orders = info.open_orders(address)
    print(f"We try to Cancel all orders on {coin}.")
    for order in orders:
        if order["coin"] == name:
            cancel_result = exchange.cancel(name, order["oid"])
            print(cancel_result)

# Function to send a message via Telegram bot
def send_message(bot_token, user_id, message):
    message = urllib.parse.quote(message)
    send_text = 'https://api.telegram.org/bot' + bot_token + '/sendMessage?chat_id=' + user_id + '&parse_mode=Markdown&text=' + message   
    response = requests.get(send_text)
    if response.status_code != 200:
        print("Error sending message")

# Function to read stop-loss value from a file
def read(file_name):
    with open(file_name, 'r') as f:
        data = json.load(f)
    return data['sl']

# Function to write stop-loss value to a file
def write(file_name, sl):
    with open(file_name, 'w') as f:
        json.dump({'sl': sl}, f)

# Entry point of the script
if __name__ == "__main__":
    main()