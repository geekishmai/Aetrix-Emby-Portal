import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildStrictInfusePlayUrl,
  formatEpisodeCode,
  pickTargetEpisodeForContainer,
} from '../src/utils/playerSchemes.ts'

test('剧集/季目标单集选择：续看进度集优先 > 首集未看 > 第一集；无子集（如拆包孤立 series）返回 null', () => {
  assert.equal(pickTargetEpisodeForContainer([]), null)
  assert.equal(pickTargetEpisodeForContainer(null), null)

  // 1. 存在正在继续观看（PlaybackPositionTicks > 0 且未播完）的单集时，优先选最近观看的那一集
  const withResume = pickTargetEpisodeForContainer([
    { Id: 'ep1', ParentIndexNumber: 1, IndexNumber: 1, UserData: { Played: true } },
    {
      Id: 'ep2',
      ParentIndexNumber: 1,
      IndexNumber: 2,
      UserData: { PlaybackPositionTicks: 3000, Played: false, LastPlayedDate: '2026-11-06T10:00:00Z' },
    },
    {
      Id: 'ep4',
      ParentIndexNumber: 1,
      IndexNumber: 4,
      UserData: { PlaybackPositionTicks: 1200, Played: false, LastPlayedDate: '2026-11-07T08:00:00Z' },
    },
  ])
  assert.equal(withResume?.episode.Id, 'ep4')
  assert.equal(withResume?.isResume, true)
  assert.equal(withResume?.codeLabel, 'S01E04')

  // 2. 无续看记录时，选第一集尚未播完的单集
  const nextUnplayed = pickTargetEpisodeForContainer([
    { Id: 'ep1', ParentIndexNumber: 1, IndexNumber: 1, UserData: { Played: true } },
    { Id: 'ep2', ParentIndexNumber: 1, IndexNumber: 2, UserData: { Played: false } },
    { Id: 'ep3', ParentIndexNumber: 1, IndexNumber: 3, UserData: { Played: false } },
  ])
  assert.equal(nextUnplayed?.episode.Id, 'ep2')
  assert.equal(nextUnplayed?.isResume, false)
  assert.equal(nextUnplayed?.codeLabel, 'S01E02')

  // 3. 全部已看完时，回退第一集重温
  const allPlayed = pickTargetEpisodeForContainer([
    { Id: 'ep1', IndexNumber: 1, UserData: { Played: true } },
    { Id: 'ep2', IndexNumber: 2, UserData: { Played: true } },
  ])
  assert.equal(allPlayed?.episode.Id, 'ep1')
  assert.equal(allPlayed?.isResume, false)
  assert.equal(allPlayed?.codeLabel, '第 1 集')
})

test('严格单流直连唤起地址：空地址或剧集容器无流地址时返回 null，绝不下发裸 infuse://', () => {
  assert.equal(buildStrictInfusePlayUrl('', 'https://emby.fluxquata.com'), null)
  assert.equal(buildStrictInfusePlayUrl('   ', 'https://emby.fluxquata.com'), null)
  assert.equal(buildStrictInfusePlayUrl('javascript:alert(1)', 'https://emby.fluxquata.com'), null)
  assert.equal(
    buildStrictInfusePlayUrl('/emby/Videos/ep4/stream.mkv?uid=2&exp=99&sign=abc', 'https://emby.fluxquata.com'),
    'infuse://x-callback-url/play?url=https%3A%2F%2Femby.fluxquata.com%2Femby%2FVideos%2Fep4%2Fstream.mkv%3Fuid%3D2%26exp%3D99%26sign%3Dabc',
  )
  assert.equal(formatEpisodeCode({ Name: '试播集' }), '试播集')
})
