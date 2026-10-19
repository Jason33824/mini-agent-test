import { createRouter, createWebHistory } from 'vue-router'
import TaskList from './views/TaskList.vue'
import TaskDetail from './views/TaskDetail.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/tasks' },
    { path: '/tasks', component: TaskList },
    { path: '/tasks/:id', component: TaskDetail, props: true },
  ],
})
