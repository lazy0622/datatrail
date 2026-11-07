import { Alert, Button, Card, Col, Row, Skeleton, Statistic, Tag } from 'antd';
import Head from 'next/head';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

type Profile = {
  matched_order_count: number;
  orders_whose_item_rows_repeat: number;
  item_rows_before_payment_join: number;
  item_rows_after_payment_join: number;
  item_amount_before_payment_join: number | null;
  item_amount_after_naive_payment_join: number | null;
};
type ProfileResponse = { data_source: string; guard_enabled: boolean; profile: Profile };

const questions = [
  '统计 2018 年各月已交付订单的 GMV（商品金额，不含运费）与订单数，并解释统计口径。',
  '检查商品明细与支付明细的关联粒度，展示重复计数风险，然后比较安全写法的结果。',
  '分析 2018 年 7 月与 8 月已交付订单的商品金额变化，按品类分解；区分观察与原因假设。',
];
const amount = (value: number | null) =>
  value === null ? '无数据' : value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export default function Commerce() {
  const [data, setData] = useState<ProfileResponse>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await fetch('/api/v1/commerce/profile');
      if (!response.ok) throw new Error('数据源暂不可用，请检查后端服务与 Olist 数据配置。');
      setData(await response.json());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  const profile = data?.profile;

  return (
    <main className='h-full overflow-auto bg-slate-50 dark:bg-slate-950 p-6 md:p-10'>
      <Head>
        <title>DataTrail · 经营分析工作台</title>
      </Head>
      <div className='max-w-6xl mx-auto space-y-6'>
        <header className='flex flex-wrap justify-between items-start gap-4'>
          <div>
            <div className='text-sm font-semibold text-blue-600 tracking-widest mb-2'>DATATRAIL</div>
            <h1 className='text-3xl font-semibold text-slate-900 dark:text-white'>每一个经营数字，都有来路</h1>
            <p className='text-slate-500 mt-3'>
              从业务问题到查询、粒度诊断和可追溯报告。基于 DB-GPT 的电商经营分析工作台。
            </p>
          </div>
          <Button onClick={() => void load()} loading={loading}>
            刷新诊断
          </Button>
        </header>
        <div className='flex gap-2 flex-wrap'>
          <Tag color='blue'>Olist 公开历史数据</Tag>
          <Tag>币种 BRL</Tag>
          <Tag color={data?.guard_enabled ? 'green' : 'default'}>
            粒度复核 {data?.guard_enabled ? '已启用' : '未启用'}
          </Tag>
        </div>
        {error && <Alert type='error' showIcon message={error} />}
        {loading ? (
          <Skeleton active />
        ) : (
          profile && (
            <>
              <Row gutter={[16, 16]}>
                <Col xs={24} md={8}>
                  <Card>
                    <Statistic title='诊断范围内的已交付订单' value={profile.matched_order_count} suffix='笔' />
                  </Card>
                </Col>
                <Col xs={24} md={8}>
                  <Card>
                    <Statistic
                      title='关联后商品行重复的订单'
                      value={profile.orders_whose_item_rows_repeat}
                      suffix='笔'
                    />
                  </Card>
                </Col>
                <Col xs={24} md={8}>
                  <Card>
                    <Statistic
                      title='商品明细行数变化'
                      value={profile.item_rows_before_payment_join}
                      suffix={`→ ${profile.item_rows_after_payment_join.toLocaleString()}`}
                    />
                  </Card>
                </Col>
              </Row>
              <Card title='同一批订单，两种关联写法'>
                <Row gutter={[24, 16]}>
                  <Col xs={24} md={12}>
                    <div className='text-slate-500 mb-2'>仅用支付记录判断订单是否在范围内</div>
                    <div className='text-2xl font-semibold text-emerald-600'>
                      {amount(profile.item_amount_before_payment_join)} BRL
                    </div>
                  </Col>
                  <Col xs={24} md={12}>
                    <div className='text-slate-500 mb-2'>直接关联原始商品明细与支付明细</div>
                    <div className='text-2xl font-semibold text-amber-600'>
                      {amount(profile.item_amount_after_naive_payment_join)} BRL
                    </div>
                  </Col>
                </Row>
                <p className='text-slate-500 mt-5'>
                  范围：已交付，且同时存在商品和支付记录的订单。这里展示的是可复现的 SQL
                  反例，不代表模型错误率。多对多关系本身并不意味着业务查询错误。
                </p>
              </Card>
            </>
          )
        )}
        <Card title='开始一项经营分析'>
          <p className='text-slate-500 mb-4'>
            选择问题后将进入 Agent 工作区，自动带入公开数据源；发送前可继续编辑问题。
          </p>
          <div className='grid gap-3'>
            {questions.map(question => (
              <Link
                key={question}
                href={{ pathname: '/', query: { datatrail: '1', question } }}
                className='block border border-slate-200 rounded-lg p-4 text-slate-700 dark:text-slate-200 hover:border-blue-400 hover:bg-blue-50 dark:hover:bg-slate-800'
              >
                {question} <span className='text-blue-500'>→</span>
              </Link>
            ))}
          </div>
        </Card>
        <Card title='本次分析的默认口径'>
          <p className='text-slate-600 dark:text-slate-300 leading-7'>
            GMV 为商品金额之和，不含运费；默认统计已交付订单，按下单日期归属月份。支付金额与 GMV
            分开解释。跨商品、支付、评价等明细表时先确认粒度；结果不足以支持因果结论时，保留假设与缺失信息。
          </p>
        </Card>
        <footer className='text-xs text-slate-400 pb-6'>
          DataTrail 个人改造：关联诊断、SQL 粒度复核与此工作台。Agent 编排、报告与看板能力复用 DB-GPT。
        </footer>
      </div>
    </main>
  );
}
