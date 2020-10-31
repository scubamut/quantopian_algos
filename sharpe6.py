#https://www.quantopian.com/posts/robinhood-based-non-day-trading-algo-yes-i-can-still-trade-on-robinhood#59e6be1fab5a1e0009f22c1e
#Modifications of Charles Witt's work
#Prevents day trading, to offset risk, its allowed to hold at most 100 stocks, it will only sell or be allowed to sell a stock the following day. After 6 days, I sell the stock either way.

import numpy as np #needed for NaN handling
import math #ceil and floor are useful for rounding

from itertools import cycle

from quantopian.pipeline import Pipeline
from quantopian.algorithm import attach_pipeline, pipeline_output
from quantopian.pipeline.data.builtin import USEquityPricing
from quantopian.pipeline.data import morningstar
from quantopian.pipeline.factors import SimpleMovingAverage, AverageDollarVolume
from quantopian.pipeline.filters.morningstar import IsPrimaryShare

#from zipline.pipeline import Pipeline
#from zipline.api import attach_pipeline, pipeline_output
#from zipline.pipeline.data import USEquityPricing
#from zipline.pipeline.data import morningstar
#from zipline.pipeline.factors import SimpleMovingAverage, AverageDollarVolume
#from zipline.pipeline.filters.morningstar import IsPrimaryShare

#from zipline.api import (
#    get_environment,
#    attach_pipeline,
#    pipeline_output,
#    order_target_percent,
#    record,
#    schedule_function,
#    date_rules,
#    time_rules,
#    get_open_orders,
#    order,
#    cancel_order,
#)
#from zipline.finance import slippage, commission
#from zipline.finance.execution import (
#    LimitOrder,
    #MarketOrder,
    #StopLimitOrder,
    #StopOrder,
#)
#import logbook
#logbook.StderrHandler().push_application()
#log = logbook.Logger('Algorithm')

#-------------------------------------------------------------------------------------------------

def initialize(context):
    #set_commission(commission.PerShare(cost=0.01, min_trade_cost=1.50))
    set_slippage(slippage.VolumeShareSlippage(volume_limit=.20, price_impact=0.0))
    #set_slippage(slippage.FixedSlippage(spread=0.00))
    set_commission(commission.PerTrade(cost=0.00))
    #set_slippage(slippage.FixedSlippage(spread=0.00))
    set_long_only()

    context.MaxCandidates=100
    context.MaxBuyOrdersAtOnce=30
    context.MyLeastPrice=3.00
    context.MyMostPrice=20.00
    context.MyFireSalePrice=context.MyLeastPrice
    context.MyFireSaleAge=6

    # over simplistic tracking of position age
    context.age={}
    print len(context.portfolio.positions)

    # Rebalance
    EveryThisManyMinutes=10
    TradingDayHours=6.5
    TradingDayMinutes=int(TradingDayHours*60)
    for minutez in xrange(
        1, 
        TradingDayMinutes, 
        EveryThisManyMinutes
    ):
        schedule_function(my_rebalance, date_rules.every_day(), time_rules.market_open(minutes=minutez))

    # Prevent excessive logging of canceled orders at market close.
    schedule_function(cancel_open_orders, date_rules.every_day(), time_rules.market_close(hours=0, minutes=1))

    # Record variables at the end of each day.
    schedule_function(my_record_vars, date_rules.every_day(), time_rules.market_close())

    # Create our pipeline and attach it to our algorithm.
    my_pipe = make_pipeline(context)
    attach_pipeline(my_pipe, 'my_pipeline')

#---------------------------------------------------------------------------------------------------------------

def make_pipeline(context):
    """
    Create our pipeline.
    """

    # At least a certain price
    price = USEquityPricing.close.latest
    AtLeastPrice   = (price >= context.MyLeastPrice)
    AtMostPrice    = (price <= context.MyMostPrice)

    # Filter for primary share equities. IsPrimaryShare is a built-in filter.
    primary_share = IsPrimaryShare()

    # Equities listed as common stock (as opposed to, say, preferred stock).
    # 'ST00000001' indicates common stock.
    common_stock = morningstar.share_class_reference.security_type.latest.eq('ST00000001')

    # Non-depositary receipts. Recall that the ~ operator inverts filters,
    # turning Trues into Falses and vice versa
    not_depositary = ~morningstar.share_class_reference.is_depositary_receipt.latest

    # Equities not trading over-the-counter.
    not_otc = ~morningstar.share_class_reference.exchange_id.latest.startswith('OTC')

    # Not when-issued equities.
    not_wi = ~morningstar.share_class_reference.symbol.latest.endswith('.WI')

    # Equities without LP in their name, .matches does a match using a regular
    # expression
    not_lp_name = ~morningstar.company_reference.standard_name.latest.matches('.* L[. ]?P.?$')

    # Equities with a null value in the limited_partnership Morningstar
    # fundamental field.
    not_lp_balance_sheet = morningstar.balance_sheet.limited_partnership.latest.isnull()

    # Equities whose most recent Morningstar market cap is not null have
    # fundamental data and therefore are not ETFs.
    have_market_cap = morningstar.valuation.market_cap.latest.notnull()

    # Filter for stocks that pass all of our previous filters.
    tradeable_stocks = (
        AtLeastPrice
        & AtMostPrice
        & primary_share
        & common_stock
        & not_depositary
        & not_otc
        & not_wi
        & not_lp_name
        & not_lp_balance_sheet
        & have_market_cap
    )

    LowVar=6
    HighVar=40

    log.info('\nAlgorithm initialized variables:\n context.MaxCandidates %s \n LowVar %s \n HighVar %s'
        % (context.MaxCandidates, LowVar, HighVar)
    )

    # High dollar volume filter.
    base_universe = AverageDollarVolume(
        window_length=20,
        mask=tradeable_stocks
    ).percentile_between(LowVar, HighVar)

    # Short close price average.
    ShortAvg = SimpleMovingAverage(
        inputs=[USEquityPricing.close],
        window_length=3,
        mask=base_universe
    )

    # Long close price average.
    LongAvg = SimpleMovingAverage(
        inputs=[USEquityPricing.close],
        window_length=45,
        mask=base_universe
    )

    percent_difference = (ShortAvg - LongAvg) / LongAvg

    # Filter to select securities to long.
    stocks_worst = percent_difference.bottom(context.MaxCandidates)
    securities_to_trade = (stocks_worst)

    return Pipeline(
        columns={
            'stocks_worst': stocks_worst
        },
        screen=(securities_to_trade),
    )

#---------------------------------------------------------------------------------------------------------------

def my_compute_weights(context):
    """
    Compute ordering weights.
    """
    # Compute even target weights for our long positions and short positions.
    div = len(context.stocks_worst)
    if div != 0: # FSC
        stocks_worst_weight = 1.00/len(context.stocks_worst)
    else: # FSC
        stocks_worst_weight = 1.00 # FSC

    return stocks_worst_weight

#---------------------------------------------------------------------------------------------------------------

def before_trading_start(context, data):
    # Gets our pipeline output every day.
    context.output = pipeline_output('my_pipeline')

    context.stocks_worst = context.output[context.output['stocks_worst']].index.tolist()

    context.stocks_worst_weight = my_compute_weights(context)

    context.MyCandidate = cycle(context.stocks_worst)
    
    context.LowestPrice=context.MyLeastPrice #reset beginning of day
    print ("context.portfolio.positions: " + str(len(context.portfolio.positions))) # FSC
    for stock in context.portfolio.positions:
        CurrPrice = float(data.current([stock], 'price'))
        if CurrPrice<context.LowestPrice:
            context.LowestPrice = CurrPrice
        if stock in context.age:
            context.age[stock] += 1
        else:
            context.age[stock] = 1
            
    for stock in context.age:
        if stock not in context.portfolio.positions:
            context.age[stock] = 0
        message = 'stock.symbol: {symbol}  :  age: {age}'
        log.info(message.format(symbol=stock.symbol, age=context.age[stock]))
    pass

    #https://www.quantopian.com/posts/calling-stocks-from-pipeline-output
    context.output= pipeline_output('my_pipeline')
    #print context.output.index
    log.info(context.output.index)
    #for stock in context.output.index:  
    #    log.info(stock.symbol)

#---------------------------------------------------------------------------------------------------------------

def my_rebalance(context, data):
    BuyFactor=.99
    SellFactor=1.01
    cash=context.portfolio.cash

    cancel_open_buy_orders(context, data)

    # Order sell at profit target in hope that somebody actually buys it
    for stock in context.portfolio.positions:
        if not get_open_orders(stock):
            StockShares = context.portfolio.positions[stock].amount
            CurrPrice = float(data.current([stock], 'price'))
            CostBasis = float(context.portfolio.positions[stock].cost_basis)
            SellPrice = float(make_div_by_05(CostBasis*SellFactor, buy=False))
            
            
            if np.isnan(SellPrice) :
                pass # probably best to wait until nan goes away
            elif (stock in context.age and context.age[stock] == 1) :
                pass
            elif (
                stock in context.age 
                and context.MyFireSaleAge<=context.age[stock] 
                and (
                    context.MyFireSalePrice>CurrPrice
                    or CostBasis>CurrPrice
                )
            ):
                if (stock in context.age and context.age[stock] < 2) :
                    pass
                elif stock not in context.age:
                    context.age[stock] = 1
                else:
                    SellPrice = float(make_div_by_05(.95*CurrPrice, buy=False))
                    order(stock, -StockShares,
                        style=LimitOrder(SellPrice)
                    )
            else:
                if (stock in context.age and context.age[stock] < 2) :
                    pass
                elif stock not in context.age:
                    context.age[stock] = 1
                else:
                
                    order(stock, -StockShares,
                        style=LimitOrder(SellPrice)
                    )

    WeightThisBuyOrder=float(1.00/context.MaxBuyOrdersAtOnce)
    for ThisBuyOrder in range(context.MaxBuyOrdersAtOnce):
        stock = context.MyCandidate.next()
        PH = data.history([stock], 'price', 20, '1d')
        PH_Avg = float(PH.mean())
        CurrPrice = float(data.current([stock], 'price'))
        if np.isnan(CurrPrice):
            pass # probably best to wait until nan goes away
        else:
            if CurrPrice > float(1.25*PH_Avg):
                BuyPrice=float(CurrPrice)
            else:
                BuyPrice=float(CurrPrice*BuyFactor)
            BuyPrice=float(make_div_by_05(BuyPrice, buy=True))
            StockShares = int(WeightThisBuyOrder*cash/BuyPrice)
            order(stock, StockShares,
                style=LimitOrder(BuyPrice)
            )

#---------------------------------------------------------------------------------------------------------------

#if cents not divisible by .05, round down if buy, round up if sell
def make_div_by_05(s, buy=False):
    s *= 20.00
    s =  math.floor(s) if buy else math.ceil(s)
    s /= 20.00
    return s

def my_record_vars(context, data):
    """
    Record variables at the end of each day.
    """

    # Record our variables.
    record(leverage=context.account.leverage)
    record(positions=len(context.portfolio.positions))
    if 0<len(context.age):
        MaxAge=context.age[max(context.age.keys(), key=(lambda k: context.age[k]))]
        print MaxAge
        record(MaxAge=MaxAge)
    record(LowestPrice=context.LowestPrice)

def log_open_order(StockToLog):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.iteritems():
        if stock == StockToLog:
            for order in orders:
                message = 'Found open order for {amount} shares in {stock}'
                log.info(message.format(amount=order.amount, stock=stock))

def log_open_orders():
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.iteritems():
        for order in orders:
            message = 'Found open order for {amount} shares in {stock}'
            log.info(message.format(amount=order.amount, stock=stock))

def cancel_open_buy_orders(context, data):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.iteritems():
        for order in orders:
            #message = 'Canceling order of {amount} shares in {stock}'
            #log.info(message.format(amount=order.amount, stock=stock))
            if 0<order.amount: #it is a buy order
                cancel_order(order)

def cancel_open_orders(context, data):
    oo = get_open_orders()
    if len(oo) == 0:
        return
    for stock, orders in oo.iteritems():
        for order in orders:
            #message = 'Canceling order of {amount} shares in {stock}'
            #log.info(message.format(amount=order.amount, stock=stock))
            cancel_order(order)

# This is the every minute stuff
def handle_data(context, data):
    pass